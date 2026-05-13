"""
Lexical BM25-based retrieval index.

This module provides fast lexical search using BM25 ranking.
"""

import logging
import math
import re
from collections import defaultdict
from typing import Dict, List, Set

from umbrella.retrieval.models import (
    Chunk,
    RetrievalQuery,
    RetrievalHit,
    HitType,
    SourceType,
)
from umbrella.retrieval.chunking import chunk_document

log = logging.getLogger(__name__)


class BM25Index:
    """
    BM25-based lexical search index.

    Implements the BM25 ranking algorithm for document retrieval.
    Uses only Python standard library for portability.
    """

    def __init__(
        self,
        k1: float = 1.5,
        b: float = 0.75,
    ):
        """
        Initialize the BM25 index.

        Args:
            k1: Term frequency saturation parameter
            b: Length normalization parameter
        """
        self.k1 = k1
        self.b = b

        # Index data
        self.documents: dict[str, Chunk] = {}
        self.doc_freqs: dict[str, int] = defaultdict(int)
        self.doc_lengths: dict[str, int] = {}
        self.avg_doc_length: float = 0.0

        # Tokenizer
        self._tokenizer = re.compile(r"\b\w+\b")

    def index_chunks(self, chunks: list[Chunk]) -> None:
        """
        Index a list of chunks.

        Args:
            chunks: List of chunks to index
        """
        # Store documents
        for chunk in chunks:
            self.documents[chunk.chunk_id] = chunk

        # Compute document frequencies
        term_doc_sets: dict[str, set[str]] = defaultdict(set)

        for chunk in chunks:
            tokens = self._tokenize(chunk.content)
            self.doc_lengths[chunk.chunk_id] = len(tokens)
            term_doc_sets[chunk.chunk_id] = set(tokens)

            for token in set(tokens):
                self.doc_freqs[token] += 1

        # Compute average document length
        if self.doc_lengths:
            self.avg_doc_length = sum(self.doc_lengths.values()) / len(self.doc_lengths)

        log.debug(
            f"Indexed {len(chunks)} chunks with {len(self.doc_freqs)} unique terms"
        )

    def search(
        self,
        query: RetrievalQuery,
        max_results: int = 10,
    ) -> list[RetrievalHit]:
        """
        Search the index using BM25 ranking.

        Args:
            query: The retrieval query
            max_results: Maximum number of results to return

        Returns:
            List of ranked RetrievalHit objects.
        """
        # Tokenize query
        query_terms = self._tokenize(query.query)
        if not query_terms:
            return []

        # Compute BM25 scores
        scores = []

        for doc_id, chunk in self.documents.items():
            score = self._compute_bm25_score(doc_id, query_terms)
            if score > 0:
                scores.append((doc_id, score))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)

        # Create hits
        hits = []
        for doc_id, score in scores[:max_results]:
            chunk = self.documents[doc_id]

            # Create excerpt
            excerpt = self._create_excerpt(chunk.content, query_terms)

            hit = RetrievalHit(
                hit_id=f"lexical_{doc_id}",
                hit_type=HitType.DOCUMENT_CHUNK,
                score=score,
                source_id=chunk.source_id,
                source_type=self._infer_source_type(chunk),
                title=chunk.metadata.get("source_title", ""),
                content=chunk.content,
                excerpt=excerpt,
                metadata={
                    "chunk_id": chunk.chunk_id,
                    "chunk_type": chunk.chunk_type,
                },
            )
            hits.append(hit)

        return hits

    def _tokenize(self, text: str) -> list[str]:
        """Tokenize text into terms."""
        tokens = self._tokenizer.findall(text.lower())
        return tokens

    def _compute_bm25_score(
        self,
        doc_id: str,
        query_terms: list[str],
    ) -> float:
        """Compute BM25 score for a document."""
        doc_len = self.doc_lengths.get(doc_id, 0)

        # Normalize document length
        doc_len_norm = (1 - self.b) + self.b * (doc_len / (self.avg_doc_length + 1e-8))

        score = 0.0
        token_counts = defaultdict(int)

        # Count term frequencies in document
        for token in self._tokenize(self.documents[doc_id].content):
            token_counts[token] += 1

        for term in query_terms:
            if term not in self.doc_freqs:
                continue

            # Term frequency
            tf = token_counts[term]

            # Document frequency with IDF
            df = self.doc_freqs[term]
            N = len(self.documents)
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1.0)

            # BM25 formula
            score += idf * (tf * (self.k1 + 1)) / (tf + self.k1 * doc_len_norm)

        return score

    def _create_excerpt(
        self,
        content: str,
        query_terms: list[str],
        max_length: int = 200,
    ) -> str:
        """Create an excerpt showing context around query terms."""
        # Find best matching sentence
        sentences = re.split(r"[.!?]\s+", content)

        best_sentence = ""
        best_score = 0

        for sentence in sentences:
            score = sum(1 for term in query_terms if term.lower() in sentence.lower())
            if score > best_score:
                best_score = score
                best_sentence = sentence

        if best_sentence:
            excerpt = best_sentence[:max_length]
            if len(best_sentence) > max_length:
                excerpt += "..."
            return excerpt

        # Fallback: first part of content
        return content[:max_length] + ("..." if len(content) > max_length else "")

    def _infer_source_type(self, chunk: Chunk) -> SourceType:
        """Infer source type from chunk metadata."""
        source_type_str = chunk.metadata.get("source_type", "")
        try:
            return SourceType(source_type_str)
        except ValueError:
            return SourceType.DOCUMENTATION


def build_lexical_index(
    sources: list,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> BM25Index:
    """
    Build a lexical index from source documents.

    Args:
        sources: List of SourceDocument objects
        chunk_size: Size for chunking documents
        chunk_overlap: Overlap between chunks

    Returns:
        A BM25Index populated with chunked documents.
    """
    index = BM25Index()
    all_chunks = []

    for source in sources:
        chunks = chunk_document(source, chunk_size, chunk_overlap)
        all_chunks.extend(chunks)

    index.index_chunks(all_chunks)
    return index


def search_lexical(
    index: BM25Index,
    query: RetrievalQuery,
    max_results: int = 10,
) -> list[RetrievalHit]:
    """
    Perform lexical search on a BM25 index.

    Args:
        index: The BM25 index to search
        query: The retrieval query
        max_results: Maximum results to return

    Returns:
        List of RetrievalHit objects.
    """
    return index.search(query, max_results)
