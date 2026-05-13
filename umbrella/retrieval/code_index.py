"""
Code symbol index with cross-referencing to docs, examples, and tests.

This module provides symbol-aware search that links symbols to their
defining files, documentation, examples, tests, and workspace usage.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from umbrella.retrieval.models import (
    SymbolRecord,
    SymbolType,
    SourceDocument,
)
from umbrella.retrieval.docs_index import DocsIndex
from umbrella.retrieval.workspace_usage import WorkspaceUsageIndex

log = logging.getLogger(__name__)


@dataclass
class LinkedSymbol:
    """
    A symbol with cross-references to docs, examples, tests, and usage.

    Enhanced version of SymbolRecord with relationship links.
    """

    symbol_name: str
    symbol_type: SymbolType
    defining_file: Path
    line_number: int
    docstring: str = ""
    signature: str = ""

    # Cross-references
    doc_links: list[str] = field(default_factory=list)
    example_links: list[str] = field(default_factory=list)
    test_links: list[str] = field(default_factory=list)
    workspace_usage_links: list[str] = field(default_factory=list)  # workspace_ids

    # Parent context
    parent_class: str | None = None
    parent_module: str | None = None

    # Metadata
    is_public: bool = True
    is_deprecated: bool = False

    def to_record(self) -> SymbolRecord:
        """Convert to SymbolRecord for backward compatibility."""
        return SymbolRecord(
            symbol_id=f"{self.defining_file}::{self.symbol_name}",
            symbol_name=self.symbol_name,
            symbol_type=self.symbol_type,
            source_id=str(self.defining_file),
            path=self.defining_file,
            line_number=self.line_number,
            docstring=self.docstring,
            signature=self.signature,
            parent_class=self.parent_class,
            parent_module=self.parent_module,
            is_public=self.is_public,
            doc_links=self.doc_links,
            example_links=self.example_links,
            test_links=self.test_links,
        )


class CodeSymbolIndex:
    """
    Index of code symbols with cross-references.

    Provides symbol search that links to documentation, examples,
    tests, and real workspace usage.
    """

    def __init__(
        self,
        docs_index: DocsIndex | None = None,
        workspace_index: WorkspaceUsageIndex | None = None,
    ):
        """
        Initialize the code symbol index.

        Args:
            docs_index: Optional docs index for linking
            workspace_index: Optional workspace usage index for linking
        """
        self._symbols: dict[str, LinkedSymbol] = {}
        self._by_name: dict[str, list[LinkedSymbol]] = {}
        self._by_module: dict[str, list[LinkedSymbol]] = {}
        self._by_class: dict[str, list[LinkedSymbol]] = {}

        self.docs_index = docs_index
        self.workspace_index = workspace_index

    def index_symbols(self, symbols: list[SymbolRecord]) -> None:
        """
        Index a list of symbols.

        Args:
            symbols: List of SymbolRecord objects
        """
        for symbol in symbols:
            linked = LinkedSymbol(
                symbol_name=symbol.symbol_name,
                symbol_type=symbol.symbol_type,
                defining_file=symbol.path,
                line_number=symbol.line_number,
                docstring=symbol.docstring or "",
                signature=symbol.signature or "",
                parent_class=symbol.parent_class,
                parent_module=symbol.parent_module,
                is_public=symbol.is_public,
            )

            # Use symbol_id as unique key
            key = symbol.symbol_id
            self._symbols[key] = linked

            # Index by name
            if symbol.symbol_name not in self._by_name:
                self._by_name[symbol.symbol_name] = []
            self._by_name[symbol.symbol_name].append(linked)

            # Index by module
            if symbol.parent_module:
                if symbol.parent_module not in self._by_module:
                    self._by_module[symbol.parent_module] = []
                self._by_module[symbol.parent_module].append(linked)

            # Index by class
            if symbol.parent_class:
                if symbol.parent_class not in self._by_class:
                    self._by_class[symbol.parent_class] = []
                self._by_class[symbol.parent_class].append(linked)

        log.debug(f"Indexed {len(symbols)} symbols")

    def link_to_docs(self) -> None:
        """Link symbols to their documentation pages."""
        if not self.docs_index:
            return

        for symbol in self._symbols.values():
            # Look for docs mentioning this symbol
            for doc_id, doc in self.docs_index.docs.items():
                content = doc.content.lower()
                if symbol.symbol_name.lower() in content:
                    rel_path = str(doc.path.relative_to(doc.path.anchor or Path("/")))
                    symbol.doc_links.append(rel_path)

        log.info("Linked symbols to docs")

    def link_to_examples(self, example_sources: list[SourceDocument]) -> None:
        """Link symbols to example files."""
        for symbol in self._symbols.values():
            for example in example_sources:
                content = example.content.lower()
                if symbol.symbol_name.lower() in content:
                    rel_path = str(
                        example.path.relative_to(example.path.anchor or Path("/"))
                    )
                    symbol.example_links.append(rel_path)

        log.info("Linked symbols to examples")

    def link_to_tests(self, test_sources: list[SourceDocument]) -> None:
        """Link symbols to test files."""
        for symbol in self._symbols.values():
            for test in test_sources:
                content = test.content.lower()
                if symbol.symbol_name.lower() in content:
                    rel_path = str(test.path.relative_to(test.path.anchor or Path("/")))
                    symbol.test_links.append(rel_path)

        log.info("Linked symbols to tests")

    def link_to_workspace_usage(self) -> None:
        """Link symbols to workspace usage."""
        if not self.workspace_index:
            return

        for symbol_name, symbols in self._by_name.items():
            workspace_ids = self.workspace_index.find_workspaces_using_symbol(
                symbol_name
            )
            for linked in symbols:
                linked.workspace_usage_links.extend(workspace_ids)

        log.info("Linked symbols to workspace usage")

    def build_all_links(
        self,
        example_sources: list[SourceDocument] | None = None,
        test_sources: list[SourceDocument] | None = None,
    ) -> None:
        """
        Build all cross-reference links.

        Args:
            example_sources: Optional example sources to link
            test_sources: Optional test sources to link
        """
        self.link_to_docs()
        if example_sources:
            self.link_to_examples(example_sources)
        if test_sources:
            self.link_to_tests(test_sources)
        self.link_to_workspace_usage()

    def get_symbol(self, symbol_id: str) -> LinkedSymbol | None:
        """Get a symbol by its ID."""
        return self._symbols.get(symbol_id)

    def get_symbols_by_name(self, name: str) -> list[LinkedSymbol]:
        """Get all symbols with a given name."""
        return self._by_name.get(name, [])

    def get_symbols_in_module(self, module: str) -> list[LinkedSymbol]:
        """Get all symbols in a module."""
        return self._by_module.get(module, [])

    def get_symbols_in_class(self, class_name: str) -> list[LinkedSymbol]:
        """Get all symbols (methods) in a class."""
        return self._by_class.get(class_name, [])

    def find_symbols(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[LinkedSymbol]:
        """
        Find symbols matching a query.

        Args:
            query: Search query
            max_results: Maximum results

        Returns:
            List of matching LinkedSymbols
        """
        query_lower = query.lower()
        results = []

        for symbol in self._symbols.values():
            score = 0.0

            # Name match
            if query_lower in symbol.symbol_name.lower():
                score += 10.0

            # Docstring match
            if query_lower in symbol.docstring.lower():
                score += 5.0

            # Module/class context
            if symbol.parent_module and query_lower in symbol.parent_module.lower():
                score += 3.0
            if symbol.parent_class and query_lower in symbol.parent_class.lower():
                score += 3.0

            if score > 0:
                results.append((symbol, score))

        # Sort by score
        results.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in results[:max_results]]

    def get_symbol_context(self, symbol_name: str) -> dict[str, Any]:
        """
        Get rich context for a symbol.

        Args:
            symbol_name: Name of the symbol

        Returns:
            Dict with defining location, docs, examples, tests, usage
        """
        symbols = self.get_symbols_by_name(symbol_name)
        if not symbols:
            return {}

        primary = symbols[0]
        return {
            "symbol_name": primary.symbol_name,
            "symbol_type": primary.symbol_type.value,
            "defining_file": str(primary.defining_file),
            "line_number": primary.line_number,
            "signature": primary.signature,
            "docstring": primary.docstring[:500] if primary.docstring else "",
            "doc_links": primary.doc_links[:5],
            "example_links": primary.example_links[:5],
            "test_links": primary.test_links[:5],
            "workspace_usage": primary.workspace_usage_links,
            "parent_class": primary.parent_class,
            "parent_module": primary.parent_module,
        }


def build_code_index(
    symbols: list[SymbolRecord],
    docs_index: DocsIndex | None = None,
    workspace_index: WorkspaceUsageIndex | None = None,
    example_sources: list[SourceDocument] | None = None,
    test_sources: list[SourceDocument] | None = None,
) -> CodeSymbolIndex:
    """
    Build a code symbol index with cross-references.

    Args:
        symbols: List of symbols to index
        docs_index: Optional docs index
        workspace_index: Optional workspace usage index
        example_sources: Optional example sources for linking
        test_sources: Optional test sources for linking

    Returns:
        Populated CodeSymbolIndex
    """
    index = CodeSymbolIndex(
        docs_index=docs_index,
        workspace_index=workspace_index,
    )
    index.index_symbols(symbols)
    index.build_all_links(example_sources, test_sources)

    return index
