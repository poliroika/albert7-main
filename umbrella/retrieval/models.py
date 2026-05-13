"""
Core data models for the retrieval system.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


class SourceType(str, Enum):
    """Types of source documents."""

    DOCUMENTATION = "documentation"
    SOURCE_CODE = "source_code"
    EXAMPLE = "example"
    TEST = "test"
    WORKSPACE_USAGE = "workspace_usage"
    README = "readme"


class SymbolType(str, Enum):
    """Types of code symbols."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    CONSTANT = "constant"
    IMPORT = "import"


class HitType(str, Enum):
    """Types of retrieval hits."""

    DOCUMENT_CHUNK = "document_chunk"
    CODE_SYMBOL = "code_symbol"
    EXAMPLE_SNIPPET = "example_snippet"
    TEST_CASE = "test_case"
    WORKSPACE_PATTERN = "workspace_pattern"


@dataclass(frozen=True)
class Chunk:
    """
    A chunk of text from a source document.

    Chunks are the atomic units indexed for lexical search.
    """

    chunk_id: str
    source_id: str
    content: str
    chunk_type: str
    start_line: int = 0
    end_line: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def token_count(self) -> int:
        """Estimate token count (rough approximation: words + punctuation)."""
        return len(self.content.split())

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "content": self.content[:500] + "..."
            if len(self.content) > 500
            else self.content,
            "chunk_type": self.chunk_type,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "metadata": self.metadata,
        }


@dataclass
class SourceDocument:
    """
    A source document (file) to be indexed.

    Represents a documentation file, source code file, example, or test.
    """

    source_id: str
    path: Path
    source_type: SourceType
    title: str = ""
    content: str = ""

    # Document metadata
    language: str = ""
    category: str = ""
    tags: set[str] = field(default_factory=set)

    # Relationships
    related_docs: set[str] = field(default_factory=set)
    related_symbols: set[str] = field(default_factory=set)

    # Indexing metadata
    indexed_at: datetime | None = None
    checksum: str = ""

    @property
    def exists(self) -> bool:
        """Check if source file exists."""
        return self.path.exists()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "source_id": self.source_id,
            "path": str(self.path),
            "source_type": self.source_type.value,
            "title": self.title,
            "language": self.language,
            "category": self.category,
            "tags": list(self.tags),
            "related_docs": list(self.related_docs),
            "related_symbols": list(self.related_symbols),
        }


@dataclass
class SymbolRecord:
    """
    A code symbol extracted from source code.

    Represents a class, function, method, or other code entity.
    """

    symbol_id: str
    symbol_name: str
    symbol_type: SymbolType
    source_id: str
    path: Path
    line_number: int = 0
    end_line_number: int = 0

    # Symbol metadata
    docstring: str = ""
    signature: str = ""
    decorators: list[str] = field(default_factory=list)
    parameters: list[str] = field(default_factory=list)
    return_type: str = ""
    parent_class: str | None = None
    parent_module: str = ""

    # Import/usage tracking
    imports: set[str] = field(default_factory=set)
    imported_by: set[str] = field(default_factory=set)

    # Documentation references
    doc_links: set[str] = field(default_factory=set)
    example_links: set[str] = field(default_factory=set)
    test_links: set[str] = field(default_factory=set)

    @property
    def is_public(self) -> bool:
        """Check if symbol is public (not starting with _)."""
        return not self.symbol_name.startswith("_")

    @property
    def is_method(self) -> bool:
        """Check if this is a method (has parent_class)."""
        return self.parent_class is not None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "symbol_id": self.symbol_id,
            "symbol_name": self.symbol_name,
            "symbol_type": self.symbol_type.value,
            "source_id": self.source_id,
            "path": str(self.path),
            "line_number": self.line_number,
            "end_line_number": self.end_line_number,
            "docstring": self.docstring[:200] + "..."
            if len(self.docstring) > 200
            else self.docstring,
            "signature": self.signature,
            "parent_class": self.parent_class,
            "parent_module": self.parent_module,
            "is_public": self.is_public,
            "is_method": self.is_method,
        }


@dataclass
class WorkspaceUsageRecord:
    """
    A record of how GMAS is used in an existing workspace.

    Captures actual usage patterns from workspaces that use GMAS.
    """

    usage_id: str
    workspace_id: str
    path: Path
    usage_type: str

    # What was used
    gmas_symbols: set[str] = field(default_factory=set)
    gmas_modules: set[str] = field(default_factory=set)
    gmas_patterns: set[str] = field(default_factory=set)

    # Context
    description: str = ""
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "usage_id": self.usage_id,
            "workspace_id": self.workspace_id,
            "path": str(self.path),
            "usage_type": self.usage_type,
            "gmas_symbols": list(self.gmas_symbols),
            "gmas_modules": list(self.gmas_modules),
            "gmas_patterns": list(self.gmas_patterns),
            "description": self.description,
            "snippet": self.snippet[:200] + "..."
            if len(self.snippet) > 200
            else self.snippet,
        }


@dataclass
class RetrievalQuery:
    """
    A query to the retrieval system.

    Can be a natural language question or a structured query.
    """

    query: str
    query_type: str = "natural_language"

    # Query constraints
    source_types: set[SourceType] = field(default_factory=set)
    symbol_types: set[SymbolType] = field(default_factory=set)
    max_results: int = 10

    # Context
    workspace_context: str | None = None
    task_context: str = ""

    @property
    def keywords(self) -> set[str]:
        """Extract keywords from query."""
        # Simple keyword extraction
        import re

        words = re.findall(r"\b\w+\b", self.query.lower())
        stop_words = {
            "how",
            "what",
            "where",
            "when",
            "why",
            "the",
            "a",
            "an",
            "in",
            "on",
            "at",
            "to",
            "for",
            "with",
            "from",
        }
        return set(words) - stop_words


@dataclass(frozen=True)
class RetrievalHit:
    """
    A single retrieval result.

    Represents a match from the search index.
    """

    hit_id: str
    hit_type: HitType
    score: float
    source_id: str
    source_type: SourceType

    # Content
    title: str = ""
    content: str = ""
    excerpt: str = ""

    # Location
    path: Path | None = None
    line_number: int = 0

    # Symbol info (if applicable)
    symbol_name: str | None = None
    symbol_type: SymbolType | None = None

    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_documentation(self) -> bool:
        """Check if hit is from documentation."""
        return self.source_type == SourceType.DOCUMENTATION

    @property
    def is_code(self) -> bool:
        """Check if hit is from source code."""
        return self.source_type == SourceType.SOURCE_CODE

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "hit_id": self.hit_id,
            "hit_type": self.hit_type.value,
            "score": self.score,
            "source_id": self.source_id,
            "source_type": self.source_type.value,
            "title": self.title,
            "content": self.content[:300] + "..."
            if len(self.content) > 300
            else self.content,
            "excerpt": self.excerpt,
            "path": str(self.path) if self.path else None,
            "line_number": self.line_number,
            "symbol_name": self.symbol_name,
            "symbol_type": self.symbol_type.value if self.symbol_type else None,
            "metadata": self.metadata,
        }


@dataclass
class RetrievalCard:
    """
    A structured retrieval result card for manager consumption.

    Compresses search results into actionable guidance.
    """

    query: str
    recommended_pattern: str
    key_symbols: list[str] = field(default_factory=list)
    key_files: list[str] = field(default_factory=list)
    example_usage: list[str] = field(default_factory=list)

    # Documentation references
    doc_references: list[str] = field(default_factory=list)

    # Anti-patterns to avoid
    anti_patterns: list[str] = field(default_factory=list)

    # Where to make changes in a workspace
    suggested_edit_locations: list[str] = field(default_factory=list)

    # Provenance
    hits: list[RetrievalHit] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def has_examples(self) -> bool:
        """Check if card includes example usage."""
        return len(self.example_usage) > 0

    @property
    def has_documentation(self) -> bool:
        """Check if card has documentation references."""
        return len(self.doc_references) > 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "query": self.query,
            "recommended_pattern": self.recommended_pattern,
            "key_symbols": self.key_symbols,
            "key_files": self.key_files,
            "example_usage": self.example_usage,
            "doc_references": self.doc_references,
            "anti_patterns": self.anti_patterns,
            "suggested_edit_locations": self.suggested_edit_locations,
            "confidence": self.confidence,
            "hit_count": len(self.hits),
        }


@dataclass
class RetrievalConfig:
    """
    Configuration for the retrieval system.
    """

    repo_root: Path
    gmas_path: Path

    # Indexing settings
    chunk_size: int = 500
    chunk_overlap: int = 50

    # Search settings
    default_max_results: int = 10
    bm25_k1: float = 1.5
    bm25_b: float = 0.75

    # Weights for hybrid scoring
    lexical_weight: float = 0.7
    semantic_weight: float = 0.3

    # Paths to index
    docs_paths: list[str] = field(
        default_factory=lambda: [
            "gmas/README.md",
            "gmas/QUICKSTART.md",
            "gmas/DOCUMENTATION.md",
            "gmas/mkdocs.yml",
            "gmas/docs/**",
        ]
    )
    source_paths: list[str] = field(
        default_factory=lambda: [
            "gmas/src/gmas/**",
        ]
    )
    example_paths: list[str] = field(
        default_factory=lambda: [
            "gmas/examples/**",
        ]
    )
    test_paths: list[str] = field(
        default_factory=lambda: [
            "gmas/tests/**",
        ]
    )
    workspace_paths: list[str] = field(
        default_factory=lambda: [
            "workspaces/agent_research/**",
        ]
    )
    mkdocs_path: str = "gmas/mkdocs.yml"


@dataclass
class IndexBuildReport:
    """
    Report generated after building the retrieval index.

    Provides statistics and metadata about the built index.
    """

    timestamp: str = ""
    build_duration_seconds: float = 0.0

    # Source counts
    total_sources: int = 0
    doc_sources: int = 0
    code_sources: int = 0
    example_sources: int = 0
    test_sources: int = 0
    workspace_sources: int = 0

    # Index stats
    total_chunks: int = 0
    total_symbols: int = 0
    workspace_usages_indexed: int = 0

    # mkdocs stats
    mkdocs_pages: int = 0
    mkdocs_sections: int = 0

    # Link stats
    symbols_with_doc_links: int = 0
    symbols_with_example_links: int = 0
    symbols_with_test_links: int = 0
    symbols_with_workspace_usage: int = 0

    # Errors
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp,
            "build_duration_seconds": self.build_duration_seconds,
            "total_sources": self.total_sources,
            "doc_sources": self.doc_sources,
            "code_sources": self.code_sources,
            "example_sources": self.example_sources,
            "test_sources": self.test_sources,
            "workspace_sources": self.workspace_sources,
            "total_chunks": self.total_chunks,
            "total_symbols": self.total_symbols,
            "workspace_usages_indexed": self.workspace_usages_indexed,
            "mkdocs_pages": self.mkdocs_pages,
            "mkdocs_sections": self.mkdocs_sections,
            "symbols_with_doc_links": self.symbols_with_doc_links,
            "symbols_with_example_links": self.symbols_with_example_links,
            "symbols_with_test_links": self.symbols_with_test_links,
            "symbols_with_workspace_usage": self.symbols_with_workspace_usage,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    @property
    def success(self) -> bool:
        """Check if build was successful."""
        return len(self.errors) == 0
