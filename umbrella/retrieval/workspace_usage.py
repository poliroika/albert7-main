"""
Workspace usage indexing for cross-referencing GMAS patterns.

This module scans workspace directories to find how gmas components
are actually used in practice, enabling "usage-based" retrieval.
"""

import ast
import logging
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


log = logging.getLogger(__name__)

_SKIP_DIR_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".memory",
        "__pycache__",
        "dist",
        "build",
        ".pytest_cache",
    }
)


def _should_skip_path(path: Path) -> bool:
    return any(part in _SKIP_DIR_PARTS for part in path.parts)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _workspace_usage_index_opted_in(workspace_path: Path) -> bool:
    """Return true only for workspaces approved as reusable GMAS examples."""

    config_path = workspace_path / "workspace.toml"
    if not config_path.exists():
        return False
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    for key in ("workspace_usage_index", "reusable_gmas_usage"):
        if _truthy(data.get(key)):
            return True

    for section_name in ("retrieval", "workspace_usage"):
        section = data.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in ("index", "enabled", "workspace_usage", "reusable_gmas_usage"):
            if _truthy(section.get(key)):
                return True

    return False


@dataclass
class ImportReference:
    """A reference to a gmas import."""

    module: str  # e.g., "gmas.execution.runner"
    symbol: str | None  # e.g., "MACPRunner"
    alias: str | None  # e.g., "Runner"
    line_number: int
    import_type: str  # "from", "import", "as"
    file_path: Path | None = None

    @property
    def full_name(self) -> str:
        """Get full qualified name."""
        if self.symbol:
            return f"{self.module}.{self.symbol}"
        return self.module


@dataclass
class WorkspaceUsageRecord:
    """Record of gmas usage in a workspace."""

    workspace_id: str
    path: Path
    imports: list[ImportReference] = field(default_factory=list)
    instantiations: dict[str, list[int]] = field(
        default_factory=dict
    )  # symbol -> line_numbers
    method_calls: dict[str, list[int]] = field(
        default_factory=dict
    )  # method -> line_numbers
    referenced_symbols: set[str] = field(default_factory=set)

    @property
    def top_symbols(self) -> list[str]:
        """Get most referenced symbols."""
        counts = defaultdict(int)
        for ref in self.imports:
            if ref.symbol:
                counts[ref.symbol] += 1
        return sorted(counts.keys(), key=lambda x: counts[x], reverse=True)


@dataclass
class SymbolUsage:
    """Aggregated usage info for a symbol."""

    symbol_name: str
    defining_file: Path | None = None
    doc_pages: list[str] = field(default_factory=list)
    example_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    workspace_usages: list[str] = field(default_factory=list)  # workspace_ids


class WorkspaceUsageIndex:
    """
    Index of workspace usage patterns for gmas components.

    Enables finding "real world" usage examples.
    """

    def __init__(self, repo_root: Path):
        """
        Initialize the workspace usage index.

        Args:
            repo_root: Repository root path
        """
        self.repo_root = repo_root
        self._by_workspace: dict[str, WorkspaceUsageRecord] = {}
        self._by_symbol: dict[str, SymbolUsage] = defaultdict(
            lambda: SymbolUsage(symbol_name="")
        )

    def index_workspace(self, workspace_path: Path) -> WorkspaceUsageRecord:
        """
        Index a single workspace directory.

        Args:
            workspace_path: Path to workspace directory

        Returns:
            WorkspaceUsageRecord for the workspace
        """
        workspace_id = workspace_path.name
        config_path = workspace_path / "workspace.toml"
        if config_path.exists():
            try:
                workspace_id = str(
                    tomllib.loads(config_path.read_text(encoding="utf-8")).get(
                        "workspace_id", workspace_id
                    )
                )
            except Exception:
                pass
        record = WorkspaceUsageRecord(
            workspace_id=workspace_id,
            path=workspace_path,
        )

        # Find all Python files (skip heavy / generated trees)
        py_files = [
            p
            for p in workspace_path.rglob("*.py")
            if not _should_skip_path(p.relative_to(workspace_path))
        ]

        for py_file in py_files:
            self._index_file(py_file, record)

        self._by_workspace[workspace_id] = record

        # Update symbol-level index
        for ref in record.imports:
            if ref.symbol:
                usage = self._by_symbol[ref.symbol]
                usage.symbol_name = ref.symbol
                if workspace_id not in usage.workspace_usages:
                    usage.workspace_usages.append(workspace_id)

        log.debug(f"Indexed workspace {workspace_id}: {len(record.imports)} imports")
        return record

    def index_all_workspaces(self, workspaces_root: Path) -> None:
        """
        Index all workspace directories under a root.

        Args:
            workspaces_root: Path containing workspace directories
        """
        if not workspaces_root.exists():
            log.warning(f"Workspaces root not found: {workspaces_root}")
            return

        for item in workspaces_root.iterdir():
            if not item.is_dir() or item.name.startswith("."):
                continue
            if item.name == "instances":
                for instance_dir in item.iterdir():
                    if (
                        instance_dir.is_dir()
                        and (instance_dir / "workspace.toml").exists()
                        and _workspace_usage_index_opted_in(instance_dir)
                    ):
                        self.index_workspace(instance_dir)
                continue
            if (
                (item / "workspace.toml").exists()
                and _workspace_usage_index_opted_in(item)
            ):
                self.index_workspace(item)

        log.info(f"Indexed {len(self._by_workspace)} workspaces")

    def _index_file(self, file_path: Path, record: WorkspaceUsageRecord) -> None:
        """Index a single Python file for gmas usage."""
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            log.debug("Failed to read %s: %s", file_path, e)
            return

        old_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(max(old_limit, 5000))
            try:
                tree = ast.parse(content)
            except RecursionError:
                log.warning(
                    "AST parse hit recursion limit for %s — skipping", file_path
                )
                return
            except MemoryError:
                log.warning("AST parse OOM for %s — skipping", file_path)
                return
            except SyntaxError as e:
                log.debug("Syntax error in %s: %s", file_path, e)
                return
        finally:
            sys.setrecursionlimit(old_limit)

        visitor = WorkspaceImportVisitor(file_path, record)
        visitor.visit(tree)

    def get_usage_for_symbol(self, symbol_name: str) -> SymbolUsage:
        """
        Get usage information for a symbol.

        Args:
            symbol_name: Name of the symbol (e.g., "MACPRunner")

        Returns:
            SymbolUsage with aggregated info
        """
        return self._by_symbol.get(symbol_name, SymbolUsage(symbol_name=symbol_name))

    def find_workspaces_using_symbol(self, symbol_name: str) -> list[str]:
        """
        Find workspaces that use a specific symbol.

        Args:
            symbol_name: Symbol to search for

        Returns:
            List of workspace IDs
        """
        usage = self._by_symbol.get(symbol_name)
        return usage.workspace_usages if usage else []

    def get_workspace_record(self, workspace_id: str) -> WorkspaceUsageRecord | None:
        """Get the usage record for a workspace."""
        return self._by_workspace.get(workspace_id)

    def link_symbol_to_docs(
        self,
        symbol_name: str,
        doc_pages: list[str],
    ) -> None:
        """Link a symbol to its documentation pages."""
        if symbol_name not in self._by_symbol:
            self._by_symbol[symbol_name] = SymbolUsage(symbol_name=symbol_name)
        self._by_symbol[symbol_name].doc_pages.extend(doc_pages)

    def link_symbol_to_examples(
        self,
        symbol_name: str,
        example_files: list[str],
    ) -> None:
        """Link a symbol to example files."""
        if symbol_name not in self._by_symbol:
            self._by_symbol[symbol_name] = SymbolUsage(symbol_name=symbol_name)
        self._by_symbol[symbol_name].example_files.extend(example_files)

    def link_symbol_to_tests(
        self,
        symbol_name: str,
        test_files: list[str],
    ) -> None:
        """Link a symbol to test files."""
        if symbol_name not in self._by_symbol:
            self._by_symbol[symbol_name] = SymbolUsage(symbol_name=symbol_name)
        self._by_symbol[symbol_name].test_files.extend(test_files)


class WorkspaceImportVisitor(ast.NodeVisitor):
    """AST visitor for extracting gmas imports from workspace code."""

    def __init__(self, file_path: Path, record: WorkspaceUsageRecord):
        self.file_path = file_path
        self.record = record
        self._in_gmas_import = False

    def visit_Import(self, node: ast.Import) -> None:
        """Extract 'import gmas.xxx' statements."""
        for alias in node.names:
            if alias.name.startswith("gmas."):
                ref = ImportReference(
                    module=alias.name,
                    symbol=None,
                    alias=alias.asname,
                    line_number=node.lineno,
                    import_type="import",
                    file_path=self.file_path,
                )
                self.record.imports.append(ref)
                self.record.referenced_symbols.add(alias.name)

        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Extract 'from gmas.xxx import yyy' statements."""
        if node.module and node.module.startswith("gmas"):
            for alias in node.names:
                ref = ImportReference(
                    module=node.module,
                    symbol=alias.name,
                    alias=alias.asname,
                    line_number=node.lineno,
                    import_type="from",
                    file_path=self.file_path,
                )
                self.record.imports.append(ref)

                # Track the symbol
                symbol_name = alias.asname or alias.name
                self.record.referenced_symbols.add(symbol_name)

                # Track instantiations
                self._track_symbol_usage(symbol_name, node.lineno)

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Track class definitions for inheritance."""
        # Check if inheriting from gmas classes
        for base in node.bases:
            base_name = self._get_name(base)
            if base_name and base_name in self.record.referenced_symbols:
                if base_name not in self.record.instantiations:
                    self.record.instantiations[base_name] = []
                self.record.instantiations[base_name].append(node.lineno)

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Track function/method calls."""
        func_name = self._get_name(node.func)
        if func_name and func_name in self.record.referenced_symbols:
            if func_name not in self.record.method_calls:
                self.record.method_calls[func_name] = []
            self.record.method_calls[func_name].append(node.lineno)

        self.generic_visit(node)

    def _track_symbol_usage(self, symbol_name: str, line_no: int) -> None:
        """Track that a symbol is used."""
        if symbol_name not in self.record.instantiations:
            self.record.instantiations[symbol_name] = []

    def _get_name(self, node: ast.AST) -> str | None:
        """Get name from an AST node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return node.attr
        return None


def build_workspace_usage_index(
    repo_root: Path,
    workspaces_root: Path | None = None,
) -> WorkspaceUsageIndex:
    """
    Build the workspace usage index.

    Args:
        repo_root: Repository root path
        workspaces_root: Optional path to workspaces directory

    Returns:
        Populated WorkspaceUsageIndex
    """
    if workspaces_root is None:
        workspaces_root = repo_root / "workspaces"

    index = WorkspaceUsageIndex(repo_root)
    index.index_all_workspaces(workspaces_root)

    return index
