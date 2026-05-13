"""
Main retrieval service orchestrating all search methods.

This module provides the primary interface for GMAS retrieval.
"""

import logging
import time
from pathlib import Path
from typing import List, Optional

from umbrella.retrieval.models import (
    RetrievalConfig,
    RetrievalQuery,
    RetrievalCard,
    RetrievalHit,
    HitType,
    SourceDocument,
    SourceType,
    IndexBuildReport,
)
from umbrella.retrieval.sources import collect_gmas_sources
from umbrella.retrieval.lexical import build_lexical_index, search_lexical
from umbrella.retrieval.symbols import build_symbol_index, search_symbols
from umbrella.retrieval.cards import build_retrieval_card
from umbrella.retrieval.docs_index import DocsIndex, parse_mkdocs_nav
from umbrella.retrieval.code_index import CodeSymbolIndex, build_code_index
from umbrella.retrieval.workspace_usage import (
    WorkspaceUsageIndex,
    build_workspace_usage_index,
)

log = logging.getLogger(__name__)
_INDEX_CACHE: dict[tuple[str, int, int], dict[str, object]] = {}


class RetrievalService:
    """
    Main service for GMAS retrieval.

    Orchestrates lexical search, symbol search, docs index,
    workspace usage, and retrieval card generation.
    """

    def __init__(
        self,
        repo_root: Path,
        config: RetrievalConfig | None = None,
    ):
        """
        Initialize the retrieval service.

        Args:
            repo_root: Path to the repository root
            config: Optional retrieval configuration
        """
        self.repo_root = repo_root

        if config is None:
            config = RetrievalConfig(repo_root=repo_root, gmas_path=repo_root / "gmas")

        self.config = config

        # Indices
        self._sources: list[SourceDocument] | None = None
        self._lexical_index = None
        self._symbol_index = None
        self._docs_index: DocsIndex | None = None
        self._code_index: CodeSymbolIndex | None = None
        self._workspace_usage_index: WorkspaceUsageIndex | None = None

        # Build report
        self._build_report: IndexBuildReport | None = None

        # Build status
        self._is_built = False

    def build_index(self) -> IndexBuildReport:
        """
        Build all retrieval indices.

        This may take a moment for large codebases.

        Returns:
            IndexBuildReport with build statistics
        """
        if self._is_built and self._build_report:
            log.info("Index already built, skipping")
            return self._build_report

        cache_key = (
            str(self.repo_root.resolve()),
            self.config.chunk_size,
            self.config.chunk_overlap,
        )
        cached = _INDEX_CACHE.get(cache_key)
        if cached is not None:
            self._sources = cached["_sources"]  # type: ignore[assignment]
            self._lexical_index = cached["_lexical_index"]
            self._symbol_index = cached["_symbol_index"]
            self._docs_index = cached["_docs_index"]  # type: ignore[assignment]
            self._code_index = cached["_code_index"]  # type: ignore[assignment]
            self._workspace_usage_index = cached["_workspace_usage_index"]  # type: ignore[assignment]
            self._build_report = cached["_build_report"]  # type: ignore[assignment]
            self._is_built = True
            return self._build_report

        start_time = time.time()
        report = IndexBuildReport(timestamp=time.strftime("%Y-%m-%d %H:%M:%S"))

        log.info("Building GMAS retrieval index...")

        # Collect sources
        self._sources = collect_gmas_sources(self.repo_root, self.config)
        report.total_sources = len(self._sources)

        # Count by type
        for source in self._sources:
            if source.source_type == SourceType.DOCUMENTATION:
                report.doc_sources += 1
            elif source.source_type == SourceType.SOURCE_CODE:
                report.code_sources += 1
            elif source.source_type == SourceType.EXAMPLE:
                report.example_sources += 1
            elif source.source_type == SourceType.TEST:
                report.test_sources += 1
            elif source.source_type == SourceType.WORKSPACE_USAGE:
                report.workspace_sources += 1

        # Parse mkdocs navigation
        log.info("Parsing mkdocs.yml...")
        mkdocs_nav = parse_mkdocs_nav(self.repo_root / self.config.mkdocs_path)
        report.mkdocs_pages = len(mkdocs_nav.by_path)
        report.mkdocs_sections = len(mkdocs_nav.by_section)

        # Build docs index
        log.info("Building docs index...")
        self._docs_index = DocsIndex(self.config.gmas_path, mkdocs_nav)
        self._docs_index.index_docs(self._sources)

        # Build lexical index
        log.info("Building lexical index...")
        self._lexical_index = build_lexical_index(
            self._sources,
            self.config.chunk_size,
            self.config.chunk_overlap,
        )
        # Count total chunks
        report.total_chunks = len(self._lexical_index.documents)

        # Build symbol index
        log.info("Building symbol index...")
        self._symbol_index = build_symbol_index(self._sources)
        report.total_symbols = len(self._symbol_index.symbols)

        # Build workspace usage index
        log.info("Building workspace usage index...")
        try:
            self._workspace_usage_index = build_workspace_usage_index(
                self.repo_root,
                self.repo_root / "workspaces",
            )
            report.workspace_usages_indexed = len(
                self._workspace_usage_index._by_workspace
            )
        except Exception as e:
            log.warning(f"Failed to build workspace usage index: {e}")
            report.warnings.append(f"Workspace usage index failed: {e}")

        # Build code index with cross-references
        log.info("Building code index with cross-references...")
        self._code_index = build_code_index(
            [self._symbol_index.symbols[s] for s in self._symbol_index.symbols],
            docs_index=self._docs_index,
            workspace_index=self._workspace_usage_index,
            example_sources=[
                s for s in self._sources if s.source_type == SourceType.EXAMPLE
            ],
            test_sources=[s for s in self._sources if s.source_type == SourceType.TEST],
        )

        # Count cross-references
        for symbol_id in self._code_index._symbols:
            symbol = self._code_index._symbols[symbol_id]
            if symbol.doc_links:
                report.symbols_with_doc_links += 1
            if symbol.example_links:
                report.symbols_with_example_links += 1
            if symbol.test_links:
                report.symbols_with_test_links += 1
            if symbol.workspace_usage_links:
                report.symbols_with_workspace_usage += 1

        self._is_built = True
        report.build_duration_seconds = time.time() - start_time
        self._build_report = report
        _INDEX_CACHE[cache_key] = {
            "_sources": self._sources,
            "_lexical_index": self._lexical_index,
            "_symbol_index": self._symbol_index,
            "_docs_index": self._docs_index,
            "_code_index": self._code_index,
            "_workspace_usage_index": self._workspace_usage_index,
            "_build_report": self._build_report,
        }

        log.info(
            f"Index built: {report.total_sources} sources, "
            f"{report.total_symbols} symbols, "
            f"{report.total_chunks} chunks in {report.build_duration_seconds:.2f}s"
        )

        return report

    def search(
        self,
        query: str,
        max_results: int = 10,
        build_card: bool = True,
    ) -> RetrievalCard:
        """
        Search GMAS documentation and code.

        Args:
            query: Natural language query
            max_results: Maximum number of results
            build_card: Whether to build a retrieval card

        Returns:
            A RetrievalCard with structured guidance, or raw hits if build_card=False
        """
        # Ensure index is built
        if not self._is_built:
            self.build_index()

        # Create retrieval query
        retrieval_query = RetrievalQuery(
            query=query,
            max_results=max_results,
        )

        # Search lexical index
        lexical_hits = search_lexical(
            self._lexical_index,
            retrieval_query,
            max_results,
        )

        # Search symbol index
        symbol_hits = search_symbols(
            self._symbol_index,
            retrieval_query,
            max_results,
        )

        workspace_usage_hits = self._search_workspace_usage(
            retrieval_query,
            max_results=max_results,
        )

        # Combine and rank hits
        all_hits = self._combine_hits(
            lexical_hits,
            symbol_hits,
            workspace_usage_hits,
            retrieval_query,
            max_results,
        )

        if build_card:
            return build_retrieval_card(retrieval_query, all_hits)
        else:
            # Return minimal card with just hits
            from umbrella.retrieval.models import RetrievalCard

            return RetrievalCard(
                query=query,
                recommended_pattern="See hits below",
                hits=all_hits[:10],
                confidence=0.5,
            )

    def _combine_hits(
        self,
        lexical_hits: list[RetrievalHit],
        symbol_hits: list[RetrievalHit],
        workspace_usage_hits: list[RetrievalHit],
        query: RetrievalQuery,
        max_results: int,
    ) -> list[RetrievalHit]:
        """Combine and rank hits from different sources."""
        combined = []

        # Add lexical hits with weight
        for hit in lexical_hits:
            weighted_hit = RetrievalHit(
                hit_id=hit.hit_id,
                hit_type=hit.hit_type,
                score=hit.score * self.config.lexical_weight,
                source_id=hit.source_id,
                source_type=hit.source_type,
                title=hit.title,
                content=hit.content,
                excerpt=hit.excerpt,
                path=hit.path,
                line_number=hit.line_number,
                symbol_name=hit.symbol_name,
                symbol_type=hit.symbol_type,
                metadata={**hit.metadata, "search_method": "lexical"},
            )
            combined.append(weighted_hit)

        # Add symbol hits with weight
        for hit in symbol_hits:
            weighted_hit = RetrievalHit(
                hit_id=hit.hit_id,
                hit_type=hit.hit_type,
                score=hit.score * self.config.semantic_weight,
                source_id=hit.source_id,
                source_type=hit.source_type,
                title=hit.title,
                content=hit.content,
                excerpt=hit.excerpt,
                path=hit.path,
                line_number=hit.line_number,
                symbol_name=hit.symbol_name,
                symbol_type=hit.symbol_type,
                metadata={**hit.metadata, "search_method": "symbol"},
            )
            combined.append(weighted_hit)

        # Add workspace usage hits with a strong boost for practical examples
        for hit in workspace_usage_hits:
            weighted_hit = RetrievalHit(
                hit_id=hit.hit_id,
                hit_type=hit.hit_type,
                score=hit.score * max(self.config.semantic_weight, 0.6),
                source_id=hit.source_id,
                source_type=hit.source_type,
                title=hit.title,
                content=hit.content,
                excerpt=hit.excerpt,
                path=hit.path,
                line_number=hit.line_number,
                symbol_name=hit.symbol_name,
                symbol_type=hit.symbol_type,
                metadata={**hit.metadata, "search_method": "workspace_usage"},
            )
            combined.append(weighted_hit)

        # Sort by combined score and deduplicate by path/line
        seen = set()
        unique_hits = []

        for hit in sorted(
            combined, key=lambda h: self._rank_hit(h, query), reverse=True
        ):
            key = (hit.path, hit.line_number)
            if key not in seen and key != (None, 0):
                unique_hits.append(hit)
                seen.add(key)

                if len(unique_hits) >= max_results:
                    break

        return unique_hits

    def _search_workspace_usage(
        self,
        query: RetrievalQuery,
        *,
        max_results: int,
    ) -> list[RetrievalHit]:
        if not self._workspace_usage_index:
            return []

        query_text = query.query.lower()
        keywords = {word.lower() for word in query.keywords}
        hits: list[RetrievalHit] = []

        for record in self._workspace_usage_index._by_workspace.values():
            for ref in record.imports:
                haystacks = [
                    ref.full_name.lower(),
                    (ref.symbol or "").lower(),
                    (ref.alias or "").lower(),
                ]
                matched_keywords = {
                    word for word in keywords if any(word in hay for hay in haystacks)
                }
                if not matched_keywords:
                    continue

                score = 8.0 + len(matched_keywords) * 3.0
                if ref.symbol and ref.symbol.lower() in query_text:
                    score += 8.0
                if "graph" in query_text and ref.symbol in {
                    "GraphBuilder",
                    "MACPRunner",
                }:
                    score += 4.0
                if "run" in query_text and ref.symbol == "MACPRunner":
                    score += 3.0

                hits.append(
                    RetrievalHit(
                        hit_id=f"workspace_usage_{record.workspace_id}_{ref.line_number}_{ref.symbol or ref.module}",
                        hit_type=HitType.WORKSPACE_PATTERN,
                        score=score,
                        source_id=record.workspace_id,
                        source_type=SourceType.WORKSPACE_USAGE,
                        title=f"{record.workspace_id}: {ref.full_name}",
                        content=f"Workspace usage in {record.workspace_id}",
                        excerpt=f"{ref.import_type} import at line {ref.line_number}",
                        path=ref.file_path or record.path,
                        line_number=ref.line_number,
                        symbol_name=ref.symbol,
                        metadata={
                            "workspace_id": record.workspace_id,
                            "module": ref.module,
                            "alias": ref.alias,
                        },
                    )
                )

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:max_results]

    def _rank_hit(self, hit: RetrievalHit, query: RetrievalQuery) -> float:
        score = hit.score
        path_str = str(hit.path).replace("\\", "/").lower() if hit.path else ""
        query_text = query.query.lower()

        source_boosts = {
            SourceType.DOCUMENTATION: 1.35,
            SourceType.README: 1.2,
            SourceType.SOURCE_CODE: 1.15,
            SourceType.WORKSPACE_USAGE: 1.3,
            SourceType.EXAMPLE: 1.05,
            SourceType.TEST: 0.45,
        }
        score *= source_boosts.get(hit.source_type, 1.0)

        if (
            "/tests/" in path_str
            or path_str.endswith("_test.py")
            or "/test_" in path_str
        ):
            score *= 0.45

        if any(
            word in query_text for word in ("how", "use", "build", "run")
        ) and hit.source_type in {
            SourceType.DOCUMENTATION,
            SourceType.SOURCE_CODE,
            SourceType.WORKSPACE_USAGE,
        }:
            score *= 1.15

        if "macprunner" in query_text and "runner" in path_str:
            score *= 1.25
        if "graph" in query_text and ("graph" in path_str or "topology" in path_str):
            score *= 1.15
        if hit.symbol_name and hit.symbol_name.lower() in query_text:
            score *= 1.2

        return score

    def get_index_stats(self) -> dict:
        """Get statistics about the built index."""
        if not self._is_built:
            return {"status": "not_built"}

        return {
            "status": "built",
            "sources_count": len(self._sources) if self._sources else 0,
            "source_types": self._count_source_types(),
            "total_chunks": len(self._lexical_index.documents)
            if self._lexical_index
            else 0,
            "total_symbols": len(self._symbol_index.symbols)
            if self._symbol_index
            else 0,
            "mkdocs_pages": len(self._docs_index.mkdocs_nav.by_path)
            if self._docs_index
            else 0,
            "workspace_count": len(self._workspace_usage_index._by_workspace)
            if self._workspace_usage_index
            else 0,
        }

    def get_build_report(self) -> IndexBuildReport | None:
        """Get the build report from the last index build."""
        return self._build_report

    def _count_source_types(self) -> dict:
        """Count documents by source type."""
        if not self._sources:
            return {}

        counts = {}
        for source in self._sources:
            stype = source.source_type.value
            counts[stype] = counts.get(stype, 0) + 1

        return counts


# Convenience function for quick lookups
def query_gmas(
    repo_root: Path,
    query: str,
    max_results: int = 10,
) -> RetrievalCard:
    """
    Quick GMAS lookup function.

    Args:
        repo_root: Path to repository root
        query: Natural language query
        max_results: Maximum results

    Returns:
        RetrievalCard with guidance.
    """
    service = RetrievalService(repo_root)
    return service.search(query, max_results=max_results)
