"""
Retrieval and search layer for GMAS framework understanding.

This module provides a hybrid retrieval system that combines:
- Lexical BM25 search over documentation and code
- Symbol-aware code indexing
- Workspace usage pattern tracking
- Structured retrieval cards for manager consumption
"""

from umbrella.retrieval.models import (
    RetrievalQuery,
    RetrievalHit,
    RetrievalCard,
    SourceDocument,
    SymbolRecord,
    WorkspaceUsageRecord,
    Chunk,
    RetrievalConfig,
    IndexBuildReport,
)
from umbrella.retrieval.docs_index import (
    MkDocsNav,
    MkDocsNavNode,
    parse_mkdocs_nav,
)
from umbrella.retrieval.service import RetrievalService
from umbrella.retrieval.sources import collect_gmas_sources
from umbrella.retrieval.lexical import BM25Index, search_lexical
from umbrella.retrieval.symbols import (
    SymbolIndex,
    extract_python_symbols,
    search_symbols,
)
from umbrella.retrieval.cards import build_retrieval_card

__all__ = [
    # Models
    "RetrievalQuery",
    "RetrievalHit",
    "RetrievalCard",
    "SourceDocument",
    "SymbolRecord",
    "WorkspaceUsageRecord",
    "Chunk",
    "RetrievalConfig",
    "IndexBuildReport",
    # MkDocs navigation
    "MkDocsNav",
    "MkDocsNavNode",
    "parse_mkdocs_nav",
    # Service
    "RetrievalService",
    # Source collection
    "collect_gmas_sources",
    # Lexical search
    "BM25Index",
    "search_lexical",
    # Symbol search
    "SymbolIndex",
    "extract_python_symbols",
    "search_symbols",
    # Cards
    "build_retrieval_card",
]
