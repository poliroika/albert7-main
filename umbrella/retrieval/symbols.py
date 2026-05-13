"""
Symbol extraction and indexing for Python code.

This module extracts classes, functions, methods, and other symbols
from Python source code using the ast module.
"""

import ast
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set

from umbrella.retrieval.models import (
    SymbolRecord,
    SymbolType,
    SourceDocument,
    RetrievalQuery,
    RetrievalHit,
    HitType,
    SourceType,
)

log = logging.getLogger(__name__)


def extract_python_symbols(
    source: SourceDocument,
) -> list[SymbolRecord]:
    """
    Extract symbols from a Python source file.

    Args:
        source: The source document (must be a .py file)

    Returns:
        List of SymbolRecord objects.
    """
    if source.path.suffix != ".py":
        return []

    try:
        content = source.content
        tree = ast.parse(content)
    except SyntaxError as e:
        log.warning(f"Failed to parse {source.path}: {e}")
        return []

    symbols = []
    visitor = SymbolExtractorVisitor(source.path, source.source_id)
    visitor.visit(tree)

    return visitor.symbols


class SymbolExtractorVisitor(ast.NodeVisitor):
    """AST visitor for extracting Python symbols."""

    def __init__(self, path: Path, source_id: str):
        self.path = path
        self.source_id = source_id
        self.symbols: list[SymbolRecord] = []
        self._current_class = None
        self._imports: set[str] = set()

    def visit_Module(self, node: ast.Module):
        """Extract module-level symbols."""
        # Get module name from path
        module_name = self._get_module_name()

        # Record module docstring
        docstring = ast.get_docstring(node)
        if docstring:
            symbol = SymbolRecord(
                symbol_id=f"{self.source_id}::module",
                symbol_name=module_name,
                symbol_type=SymbolType.MODULE,
                source_id=self.source_id,
                path=self.path,
                line_number=0,
                docstring=docstring,
                parent_module=module_name,
            )
            self.symbols.append(symbol)

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        """Extract class definitions."""
        # Save parent class context
        parent_class = self._current_class
        self._current_class = node.name

        # Get class info
        docstring = ast.get_docstring(node)
        decorators = [self._get_decorator_name(d) for d in node.decorator_list]
        bases = [self._get_name(base) for base in node.bases]

        symbol = SymbolRecord(
            symbol_id=f"{self.source_id}::class::{node.name}",
            symbol_name=node.name,
            symbol_type=SymbolType.CLASS,
            source_id=self.source_id,
            path=self.path,
            line_number=node.lineno,
            end_line_number=self._get_end_line(node),
            docstring=docstring or "",
            signature=f"class {node.name}({', '.join(bases)})",
            decorators=decorators,
            parent_module=self._get_module_name(),
            parent_class=parent_class,
        )
        self.symbols.append(symbol)

        self.generic_visit(node)

        # Restore parent class context
        self._current_class = parent_class

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """Extract function and method definitions."""
        docstring = ast.get_docstring(node)
        decorators = [self._get_decorator_name(d) for d in node.decorator_list]

        # Get parameters
        parameters = [arg.arg for arg in node.args.args]
        returns = self._get_name(node.returns) if node.returns else None

        # Build signature
        signature_parts = []
        if decorators:
            signature_parts.extend(decorators)
        signature_parts.append(f"def {node.name}")
        signature_parts.append("(" + ", ".join(parameters) + ")")
        if returns:
            signature_parts.append("-> " + returns)
        signature = " ".join(signature_parts)

        # Determine symbol type
        if self._current_class:
            symbol_type = SymbolType.METHOD
            symbol_id = (
                f"{self.source_id}::class::{self._current_class}::method::{node.name}"
            )
        else:
            symbol_type = SymbolType.FUNCTION
            symbol_id = f"{self.source_id}::function::{node.name}"

        symbol = SymbolRecord(
            symbol_id=symbol_id,
            symbol_name=node.name,
            symbol_type=symbol_type,
            source_id=self.source_id,
            path=self.path,
            line_number=node.lineno,
            end_line_number=self._get_end_line(node),
            docstring=docstring or "",
            signature=signature,
            decorators=decorators,
            parameters=parameters,
            return_type=returns or "",
            parent_class=self._current_class,
            parent_module=self._get_module_name(),
        )
        self.symbols.append(symbol)

        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """Extract async function definitions."""
        docstring = ast.get_docstring(node)
        decorators = [self._get_decorator_name(d) for d in node.decorator_list]

        # Get parameters
        parameters = [arg.arg for arg in node.args.args]
        returns = self._get_name(node.returns) if node.returns else None

        # Build signature
        signature_parts = []
        if decorators:
            signature_parts.extend(decorators)
        signature_parts.append(f"async def {node.name}")
        signature_parts.append("(" + ", ".join(parameters) + ")")
        if returns:
            signature_parts.append("-> " + returns)
        signature = " ".join(signature_parts)

        # Determine symbol type
        if self._current_class:
            symbol_type = SymbolType.METHOD
            symbol_id = (
                f"{self.source_id}::class::{self._current_class}::method::{node.name}"
            )
        else:
            symbol_type = SymbolType.FUNCTION
            symbol_id = f"{self.source_id}::function::{node.name}"

        symbol = SymbolRecord(
            symbol_id=symbol_id,
            symbol_name=node.name,
            symbol_type=symbol_type,
            source_id=self.source_id,
            path=self.path,
            line_number=node.lineno,
            end_line_number=self._get_end_line(node),
            docstring=docstring or "",
            signature=signature,
            decorators=decorators,
            parameters=parameters,
            return_type=returns or "",
            parent_class=self._current_class,
            parent_module=self._get_module_name(),
        )
        self.symbols.append(symbol)

        self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        """Extract import statements."""
        for alias in node.names:
            module_name = alias.name
            self._imports.add(module_name)

        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        """Extract from...import statements."""
        if node.module:
            for alias in node.names:
                full_name = f"{node.module}.{alias.name}"
                self._imports.add(full_name)

        self.generic_visit(node)

    def _get_module_name(self) -> str:
        """Get module name from file path."""
        # Convert path to module name
        parts = self.path.parts
        try:
            gmas_idx = parts.index("gmas")
            module_parts = parts[gmas_idx + 1 : -1]  # Skip gmas and .py
            return ".".join(module_parts)
        except ValueError:
            # Not in gmas structure
            if "src" in parts:
                src_idx = parts.index("src")
                module_parts = parts[src_idx + 1 : -1]
                return ".".join(module_parts)
            return self.path.stem

    def _get_decorator_name(self, decorator: ast.expr) -> str:
        """Get decorator name."""
        if isinstance(decorator, ast.Name):
            return decorator.id
        elif isinstance(decorator, ast.Call):
            if isinstance(decorator.func, ast.Name):
                return decorator.func.id
            elif isinstance(decorator.func, ast.Attribute):
                return self._get_name(decorator.func)
        return str(decorator)

    def _get_name(self, node: ast.AST) -> str:
        """Get name from an AST node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._get_name(node.value)}.{node.attr}"
        return ""

    def _get_end_line(self, node: ast.AST) -> int:
        """Estimate end line of a node."""
        # This is a rough estimate
        if hasattr(node, "end_lineno") and node.end_lineno:
            return node.end_lineno
        return node.lineno


class SymbolIndex:
    """
    Index of code symbols for fast lookup.

    Provides symbol-based search over extracted code symbols.
    """

    def __init__(self):
        self.symbols: dict[str, SymbolRecord] = {}
        self._name_index: dict[str, set[str]] = defaultdict(set)
        self._module_index: dict[str, set[str]] = defaultdict(set)
        self._class_index: dict[str, set[str]] = defaultdict(set)

    def index_symbols(self, symbols: list[SymbolRecord]) -> None:
        """Index a list of symbols."""
        for symbol in symbols:
            self.symbols[symbol.symbol_id] = symbol

            # Name index
            self._name_index[symbol.symbol_name.lower()].add(symbol.symbol_id)

            # Module index
            if symbol.parent_module:
                self._module_index[symbol.parent_module].add(symbol.symbol_id)

            # Class index
            if symbol.parent_class:
                self._class_index[symbol.parent_class.lower()].add(symbol.symbol_id)

        log.debug(f"Indexed {len(symbols)} symbols")

    def search(
        self,
        query: RetrievalQuery,
        max_results: int = 10,
    ) -> list[RetrievalHit]:
        """
        Search the symbol index.

        Args:
            query: The retrieval query
            max_results: Maximum results to return

        Returns:
            List of RetrievalHit objects.
        """
        hits = []
        query_lower = query.query.lower()

        # Tokenize query into words for better matching
        query_words = [w for w in query_lower.split() if len(w) > 2]

        # Search by symbol name
        for symbol_id in self.symbols:
            symbol = self.symbols[symbol_id]
            symbol_name_lower = symbol.symbol_name.lower()
            docstring_lower = symbol.docstring.lower()
            signature_lower = symbol.signature.lower()

            # Check if query matches symbol name or docstring
            score = 0.0

            # Full query match bonus
            if query_lower == symbol_name_lower:
                score += 30.0
            elif query_lower in symbol_name_lower:
                score += 18.0
            elif query_lower in docstring_lower:
                score += 10.0
            elif query_lower in signature_lower:
                score += 5.0

            # Word-level matching for multi-word queries
            for word in query_words:
                if word in symbol_name_lower:
                    score += 10.0
                if word in docstring_lower:
                    score += 3.0
                if word in signature_lower:
                    score += 2.0

            if symbol.parent_module:
                parent_module_lower = symbol.parent_module.lower()
                for word in query_words:
                    if word in parent_module_lower:
                        score += 2.5

            if symbol.is_public:
                score += 0.5

            if score > 0:
                hit = RetrievalHit(
                    hit_id=f"symbol_{symbol_id}",
                    hit_type=HitType.CODE_SYMBOL,
                    score=score,
                    source_id=symbol.source_id,
                    source_type=SourceType.SOURCE_CODE,
                    title=symbol.signature,
                    content=symbol.docstring or "",
                    excerpt=symbol.docstring[:200] if symbol.docstring else "",
                    path=symbol.path,
                    line_number=symbol.line_number,
                    symbol_name=symbol.symbol_name,
                    symbol_type=symbol.symbol_type,
                    metadata={
                        "parent_class": symbol.parent_class,
                        "parent_module": symbol.parent_module,
                        "is_public": symbol.is_public,
                    },
                )
                hits.append(hit)

        # Sort by score
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:max_results]


def build_symbol_index(sources: list[SourceDocument]) -> SymbolIndex:
    """
    Build a symbol index from source documents.

    Args:
        sources: List of SourceDocument objects

    Returns:
        A populated SymbolIndex.
    """
    index = SymbolIndex()

    allowed_types = {
        SourceType.SOURCE_CODE,
        SourceType.EXAMPLE,
        SourceType.WORKSPACE_USAGE,
    }
    for source in sources:
        if source.path.suffix == ".py" and source.source_type in allowed_types:
            symbols = extract_python_symbols(source)
            index.index_symbols(symbols)

    return index


def search_symbols(
    index: SymbolIndex,
    query: RetrievalQuery,
    max_results: int = 10,
) -> list[RetrievalHit]:
    """
    Search the symbol index.

    Args:
        index: The symbol index
        query: The retrieval query
        max_results: Maximum results to return

    Returns:
        List of RetrievalHit objects.
    """
    return index.search(query, max_results)
