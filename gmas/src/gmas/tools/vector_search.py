"""
Vector Search tool — semantic similarity search over a knowledge base.

Allows agents to index documents, search by meaning using embeddings,
and retrieve ranked results with relevance scores and citations.

Supported vector stores:
- in_memory: Pure-PyTorch store, no external dependencies
- faiss: FAISS-backed store (pip install faiss-cpu)
- qdrant: Qdrant-backed store (pip install qdrant-client)
- pinecone: Pinecone-backed store (pip install pinecone-client)
- milvus: Milvus-backed store (pip install pymilvus) [EXPERIMENTAL]

Example:
    from gmas.tools.vector_search import VectorIndexTool, VectorSearchTool

    # Create tool with defaults (in-memory store)
    search = VectorSearchTool()
    index = VectorIndexTool(
        store=search._store,
        provider=search._provider,
        chunker=search._chunker,
    )

    # Index documents
    index.index(["Python is a programming language.", "Rust is fast."])

    # Search
    result = search.execute(query="What is Python?")
    print(result.output)

    # With FAISS backend
    search = VectorSearchTool(store_type="faiss", dimension=384)

    # From environment settings (GMAS_VECTOR_* variables)
    search = VectorSearchTool.from_settings()

"""

import asyncio
import json
import re
import threading
import warnings
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self

import torch
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator

from gmas.config.logging import logger
from gmas.core.encoder import NodeEncoder
from gmas.tools.base import BaseTool, register_tool_factory

if TYPE_CHECKING:
    from gmas.tools.base import ToolResult

_CHARS_PER_TOKEN = 4

_shared_components_lock = threading.Lock()
_SHARED_VECTOR_COMPONENTS: dict[str, tuple["VectorStore", "EmbeddingProvider", "DocumentChunker"]] = {}


def clear_shared_components(key: str | None = None) -> None:
    """
    Remove cached shared vector components.

    Calls ``store.close()`` on each evicted store before removing it from
    the cache so that remote backend connections are cleaned up.

    Args:
        key: Specific ``shared_key`` prefix to remove.  All cache entries
             whose composite key starts with ``"{key}::"`` are evicted.
             When *None*, the entire cache is cleared.

    """
    with _shared_components_lock:
        if key is None:
            for store, _p, _c in _SHARED_VECTOR_COMPONENTS.values():
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    logger.opt(exception=True).debug("Error closing store during cache clear")
            _SHARED_VECTOR_COMPONENTS.clear()
        else:
            prefix = f"{key}::"
            to_remove = [k for k in _SHARED_VECTOR_COMPONENTS if k.startswith(prefix)]
            for k in to_remove:
                store, _p, _c = _SHARED_VECTOR_COMPONENTS[k]
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    logger.opt(exception=True).debug("Error closing store during cache clear for key={}", k)
                del _SHARED_VECTOR_COMPONENTS[k]


def _resolve_vector_search_defaults() -> dict[str, Any]:
    from gmas.config.settings import FrameworkSettings

    defaults: dict[str, Any] = {
        "store_type": "in_memory",
        "top_k": 5,
        "score_threshold": 0.0,
        "max_context_tokens": 4096,
        "citation_mode": "numbered",
        "strict_context_mode": False,
        "context_template": "{context}",
    }
    try:
        settings = FrameworkSettings()
    except (ValidationError, OSError):
        return defaults

    defaults.update(
        {
            "store_type": settings.vector_store_type,
            "top_k": settings.vector_top_k,
            "score_threshold": settings.vector_score_threshold,
            "max_context_tokens": settings.vector_max_context_tokens,
            "citation_mode": settings.vector_citation_mode,
            "strict_context_mode": settings.vector_strict_context,
            "context_template": settings.vector_context_template,
        }
    )
    return defaults


def _resolve_vector_index_default_store_type() -> str:
    from gmas.config.settings import FrameworkSettings

    try:
        settings = FrameworkSettings()
    except (ValidationError, OSError):
        return "in_memory"
    return settings.vector_store_type


def _normalize_components(
    *,
    overrides: dict[str, Any],
    shared: bool,
    shared_key: str | None,
    store_type: str,
    store_kwargs: dict[str, Any],
) -> tuple["VectorStore | None", "EmbeddingProvider | None", "DocumentChunker | None"]:
    store = overrides.get("store")
    provider = overrides.get("provider")
    chunker = overrides.get("chunker")

    if store is not None and not isinstance(store, VectorStore):
        msg = "store must be a VectorStore instance"
        raise TypeError(msg)
    if provider is not None and not isinstance(provider, EmbeddingProvider):
        msg = "provider must be an EmbeddingProvider instance"
        raise TypeError(msg)
    if chunker is not None and not isinstance(chunker, DocumentChunker):
        msg = "chunker must be a DocumentChunker instance"
        raise TypeError(msg)

    if shared:
        if not shared_key:
            msg = "shared_key is required when shared=True"
            raise ValueError(msg)
        all_explicit = store is not None and provider is not None and chunker is not None
        if all_explicit:
            return store, provider, chunker
        shared_store, shared_provider, shared_chunker = _shared_components(
            shared_key=shared_key,
            store_type=store_type,
            store_kwargs=store_kwargs,
        )
        return (
            store if store is not None else shared_store,
            provider if provider is not None else shared_provider,
            chunker if chunker is not None else shared_chunker,
        )
    return store, provider, chunker


def _coerce_citation_mode(value: Any) -> Literal["none", "inline", "numbered"]:
    candidate = str(value)
    if candidate == "none":
        return "none"
    if candidate == "inline":
        return "inline"
    return "numbered"


def _relevance_from_distance(distance: float) -> float:
    """Convert a non-negative distance to higher-is-better relevance score in [0, 1]."""
    d = max(0.0, float(distance))
    return 1.0 / (1.0 + d)


def _ensure_fixed_dimension(vectors: list[list[float]], expected_dim: int, *, context: str) -> None:
    if expected_dim <= 0:
        msg = f"{context}: expected_dim must be > 0, got {expected_dim}"
        raise ValueError(msg)
    bad = [i for i, v in enumerate(vectors) if len(v) != expected_dim]
    if bad:
        got = len(vectors[bad[0]])
        msg = f"{context}: embedding dimension mismatch, expected {expected_dim}, got {got} at index {bad[0]}"
        raise ValueError(msg)


def _validate_add_inputs(
    *,
    documents: list[str],
    embeddings: list[list[float]],
    metadata: list[dict[str, Any]] | None,
    context: str,
) -> None:
    if len(documents) != len(embeddings):
        msg = (
            f"{context}: documents/embeddings length mismatch, documents={len(documents)}, embeddings={len(embeddings)}"
        )
        raise ValueError(msg)
    if metadata is not None and len(metadata) != len(documents):
        msg = f"{context}: metadata/documents length mismatch, metadata={len(metadata)}, documents={len(documents)}"
        raise ValueError(msg)


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Compute embedding for a single text."""

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Compute embeddings for a batch of texts."""


class SentenceTransformerProvider(BaseModel, EmbeddingProvider):
    """Embedding provider backed by NodeEncoder (sentence-transformers)."""

    model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    batch_size: int = 32
    normalize: bool = True

    _encoder: NodeEncoder | None = PrivateAttr(default=None)
    _encoder_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _get_encoder(self) -> NodeEncoder:
        if self._encoder is not None:
            return self._encoder
        with self._encoder_lock:
            if self._encoder is None:
                self._encoder = NodeEncoder(
                    model_name=self.model_name,
                    normalize_embeddings=self.normalize,
                )
            return self._encoder

    def embed(self, text: str) -> list[float]:
        encoder = self._get_encoder()
        tensor = encoder.encode([text])
        return tensor[0].cpu().tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        encoder = self._get_encoder()
        result: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            tensors = encoder.encode(batch)
            result.extend(row.cpu().tolist() for row in tensors)
        return result

    @classmethod
    def from_settings(cls) -> "SentenceTransformerProvider":
        """Create provider from FrameworkSettings (environment variables)."""
        from gmas.config.settings import FrameworkSettings

        try:
            settings = FrameworkSettings()
        except (ValidationError, OSError):
            return cls()

        return cls(
            model_name=settings.embedding_model,
            normalize=settings.embedding_normalize,
            batch_size=settings.embedding_batch_size,
        )


class DocumentChunker(BaseModel):
    """Split text into overlapping chunks for indexing."""

    chunk_units: int = Field(default=512, description="Number of units (words/sentences/paragraphs) per chunk")
    chunk_overlap: int = Field(default=20, description="Overlap in units between chunks")
    split_strategy: Literal["words", "sentences", "paragraphs"] = Field(default="words")

    @model_validator(mode="after")
    def _validate_overlap(self) -> "DocumentChunker":
        if self.chunk_units < 1:
            msg = f"chunk_units must be >= 1, got {self.chunk_units}"
            raise ValueError(msg)
        if not 0 <= self.chunk_overlap < self.chunk_units:
            msg = (
                f"chunk_overlap must be in [0, chunk_units), got overlap={self.chunk_overlap}, units={self.chunk_units}"
            )
            raise ValueError(msg)
        return self

    def chunk(self, text: str) -> list[str]:
        """Split text into chunks according to the configured strategy."""
        if not text.strip():
            return []

        units = self._split_units(text)
        return self._merge_units(units)

    def _split_units(self, text: str) -> list[str]:
        if self.split_strategy == "paragraphs":
            return [p.strip() for p in text.split("\n\n") if p.strip()]
        if self.split_strategy == "sentences":
            return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        return text.split()

    def _merge_units(self, units: list[str]) -> list[str]:
        if not units:
            return []

        chunks: list[str] = []
        start = 0

        while start < len(units):
            end = start + self.chunk_units
            chunk_units = units[start:end]

            separator = "\n\n" if self.split_strategy == "paragraphs" else " "
            chunks.append(separator.join(chunk_units))
            step = self.chunk_units - self.chunk_overlap
            start += max(step, 1)

        return chunks


class SearchResult(BaseModel):
    """Single result from a vector search query."""

    id: str
    document: str
    score: float

    metadata: dict[str, Any] = Field(default_factory=dict)


ScoreSemantics = Literal["higher_is_better", "lower_is_better", "unknown"]


class VectorStore(ABC):
    """Abstract interface for vector storage backends."""

    @property
    def score_semantics(self) -> ScoreSemantics:
        """
        Describe how ``SearchResult.score`` should be interpreted.

        All built-in backends return scores in *higher-is-better* orientation.
        The score scale is **backend- and metric-specific**: cosine similarity
        yields [-1, 1], dot product is unbounded, and L2 is converted to
        [0, 1] via ``1/(1+d)``.  Use ``metadata["_raw_score"]`` and
        ``metadata["_score_metric"]`` for precise interpretation.

        External backends whose metric is not known at construction time
        return ``"unknown"``.
        """
        return "higher_is_better"

    def close(self) -> None:
        """
        Release resources held by the store.

        The default implementation is a no-op.  Remote backends should
        override this to close network connections.
        """
        return

    @abstractmethod
    def add(
        self,
        documents: list[str],
        embeddings: list[list[float]],
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """Add documents with embeddings, return assigned ids."""

    @abstractmethod
    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Find top_k nearest neighbours, optionally filtered by metadata."""

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        """Delete documents by id."""


class InMemoryVectorStore(VectorStore):
    """
    Pure-PyTorch in-memory vector store (no external dependencies).

    Thread-safe: all mutating operations are guarded by an internal lock.
    """

    def __init__(
        self,
        metric: Literal["cosine", "dot", "l2"] = "cosine",
    ) -> None:
        self._metric = metric
        self._ids: list[str] = []
        self._documents: list[str] = []
        self._metadata: list[dict[str, Any]] = []
        self._tensor: torch.Tensor | None = None
        self._lock = threading.RLock()

    @property
    def score_semantics(self) -> ScoreSemantics:
        return "higher_is_better"

    @property
    def ids(self) -> list[str]:
        return list(self._ids)

    def add(
        self,
        documents: list[str],
        embeddings: list[list[float]],
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        import uuid

        _validate_add_inputs(
            documents=documents,
            embeddings=embeddings,
            metadata=metadata,
            context="InMemoryVectorStore.add",
        )
        if not embeddings:
            return []
        base_dim = len(embeddings[0])
        _ensure_fixed_dimension(embeddings, base_dim, context="InMemoryVectorStore.add")

        with self._lock:
            if self._tensor is not None and self._tensor.shape[1] != base_dim:
                msg = (
                    "InMemoryVectorStore.add: embedding dimension mismatch, "
                    f"existing {self._tensor.shape[1]}, got {base_dim}"
                )
                raise ValueError(msg)

            new_ids = [str(uuid.uuid4()) for _ in documents]
            self._ids.extend(new_ids)
            self._documents.extend(documents)
            self._metadata.extend(metadata or [{} for _ in documents])

            new_tensor = torch.tensor(embeddings, dtype=torch.float32)
            if self._tensor is None:
                self._tensor = new_tensor
            else:
                self._tensor = torch.cat([self._tensor, new_tensor], dim=0)

        return new_ids

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        with self._lock:
            if not self._ids or self._tensor is None:
                return []

            if len(query_embedding) != self._tensor.shape[1]:
                msg = (
                    "InMemoryVectorStore.search: query embedding dimension mismatch, "
                    f"expected {self._tensor.shape[1]}, got {len(query_embedding)}"
                )
                raise ValueError(msg)

            query_t = torch.tensor(query_embedding, dtype=torch.float32).unsqueeze(0)

            if metadata_filters:
                mask = [self._matches_filters(m, metadata_filters) for m in self._metadata]
                indices = [i for i, ok in enumerate(mask) if ok]
                if not indices:
                    return []
                indices_t = torch.tensor(indices, dtype=torch.long)
                filtered_tensor = self._tensor[indices_t]
            else:
                indices = list(range(len(self._ids)))
                filtered_tensor = self._tensor

            if self._metric == "cosine":
                scores = torch.nn.functional.cosine_similarity(query_t, filtered_tensor)
            elif self._metric == "dot":
                scores = filtered_tensor @ query_t.squeeze(0)
            else:
                dists = torch.cdist(query_t, filtered_tensor).squeeze(0)
                scores = 1.0 / (1.0 + dists)

            k = min(top_k, len(scores))
            top_scores, top_positions = torch.topk(scores, k)

            return [
                SearchResult(
                    id=self._ids[indices[pos]],
                    document=self._documents[indices[pos]],
                    score=float(top_scores[j].item()),
                    metadata={
                        **self._metadata[indices[pos]],
                        "_raw_score": float(top_scores[j].item()),
                        "_score_metric": self._metric,
                        "_score_semantics": "higher_is_better",
                    },
                )
                for j, pos in enumerate(top_positions.tolist())
            ]

    def delete(self, ids: list[str]) -> None:
        with self._lock:
            remove = set(ids)
            keep = [i for i, doc_id in enumerate(self._ids) if doc_id not in remove]
            self._ids = [self._ids[i] for i in keep]
            self._documents = [self._documents[i] for i in keep]
            self._metadata = [self._metadata[i] for i in keep]

            if keep and self._tensor is not None:
                self._tensor = self._tensor[torch.tensor(keep, dtype=torch.long)]
            else:
                self._tensor = None

    @staticmethod
    def _matches_filters(meta: dict[str, Any], filters: dict[str, Any] | None) -> bool:
        if not filters:
            return True
        return all(meta.get(k) == v for k, v in filters.items())


class FaissVectorStore(VectorStore):
    """
    FAISS-backed vector store (requires ``pip install faiss-cpu``).

    Thread-safe: all mutating operations are guarded by an internal lock.

    .. warning::

        Metadata filtering is **approximate** (post-filter).  FAISS does not
        support native metadata predicates, so this backend over-samples by
        ``metadata_oversample`` and filters in Python.  If the relevant
        documents with matching metadata fall outside the oversampled window
        they will be missed.  For strict server-side filtering use Qdrant or
        Pinecone.
    """

    def __init__(
        self,
        dimension: int = 384,
        metric: Literal["cosine", "dot", "l2"] = "cosine",
        metadata_oversample: int = 4,
    ) -> None:
        try:
            import faiss
        except ImportError as e:
            msg = "faiss-cpu required: pip install faiss-cpu"
            raise ImportError(msg) from e

        self._faiss = faiss
        self._dimension = dimension
        self._metric = metric
        self._metadata_oversample = max(2, metadata_oversample)
        self._index = self._create_index()
        self._ids: list[str] = []
        self._documents: list[str] = []
        self._metadata: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    @property
    def score_semantics(self) -> ScoreSemantics:
        return "higher_is_better"

    def add(
        self,
        documents: list[str],
        embeddings: list[list[float]],
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        import uuid

        _validate_add_inputs(
            documents=documents,
            embeddings=embeddings,
            metadata=metadata,
            context="FaissVectorStore.add",
        )
        _ensure_fixed_dimension(embeddings, self._dimension, context="FaissVectorStore.add")

        with self._lock:
            new_ids = [str(uuid.uuid4()) for _ in documents]
            vectors = torch.tensor(embeddings, dtype=torch.float32).numpy()
            if self._metric == "cosine":
                self._faiss.normalize_L2(vectors)
            self._index.add(vectors)
            self._ids.extend(new_ids)
            self._documents.extend(documents)
            self._metadata.extend(metadata or [{} for _ in documents])
        return new_ids

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        with self._lock:
            if not self._ids:
                return []
            if len(query_embedding) != self._dimension:
                msg = (
                    "FaissVectorStore.search: query embedding dimension mismatch, "
                    f"expected {self._dimension}, got {len(query_embedding)}"
                )
                raise ValueError(msg)

            query = torch.tensor([query_embedding], dtype=torch.float32).numpy()
            if self._metric == "cosine":
                self._faiss.normalize_L2(query)

            fetch_k = min(top_k * self._metadata_oversample, len(self._ids))
            scores, indices = self._index.search(query, fetch_k)

            results: list[SearchResult] = []
            for j, idx in enumerate(indices[0]):
                if idx == -1 or len(results) >= top_k:
                    break
                meta = self._metadata[idx]
                if metadata_filters and not all(meta.get(k) == v for k, v in metadata_filters.items()):
                    continue

                raw_score = float(scores[0][j])
                score = raw_score
                if self._metric == "l2":
                    score = _relevance_from_distance(raw_score)

                results.append(
                    SearchResult(
                        id=self._ids[idx],
                        document=self._documents[idx],
                        score=score,
                        metadata={
                            **meta,
                            "_raw_score": raw_score,
                            "_score_metric": self._metric,
                            "_score_semantics": "higher_is_better",
                        },
                    )
                )

            if metadata_filters and len(results) < top_k and results:
                results[-1].metadata["_filter_warning"] = (
                    "post-filter returned fewer results than requested; some matching documents may have been missed"
                )

        return results

    def delete(self, ids: list[str]) -> None:
        with self._lock:
            remove = set(ids)
            keep = [i for i, doc_id in enumerate(self._ids) if doc_id not in remove]
            self._ids = [self._ids[i] for i in keep]
            self._documents = [self._documents[i] for i in keep]
            self._metadata = [self._metadata[i] for i in keep]

            new_index = self._create_index()
            if keep:
                vectors = torch.tensor(
                    [self._index.reconstruct(i) for i in keep],
                    dtype=torch.float32,
                ).numpy()
                new_index.add(vectors)
            self._index = new_index

    def _create_index(self) -> Any:
        if self._metric == "l2":
            return self._faiss.IndexFlatL2(self._dimension)
        return self._faiss.IndexFlatIP(self._dimension)


class QdrantVectorStore(VectorStore):
    """
    Qdrant-backed vector store (requires ``pip install qdrant-client``).

    Concurrency is delegated to the Qdrant client library.
    Call :meth:`close` when done to release the underlying HTTP connection.
    """

    _DISTANCE_MAP: ClassVar[dict[str, str]] = {
        "cosine": "COSINE",
        "dot": "DOT",
        "l2": "EUCLID",
    }

    def __init__(
        self,
        collection_name: str,
        url: str = "localhost:6333",
        dimension: int = 384,
        metric: Literal["cosine", "dot", "l2"] = "cosine",
    ) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
        except ImportError as e:
            msg = "qdrant-client required: pip install qdrant-client"
            raise ImportError(msg) from e

        self._client = QdrantClient(url=url)
        self._collection = collection_name
        self._dimension = dimension
        self._metric = metric

        qdrant_distance = getattr(Distance, self._DISTANCE_MAP.get(metric, "COSINE"))
        if not self._client.collection_exists(collection_name):
            self._client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=dimension, distance=qdrant_distance),
            )

    @property
    def score_semantics(self) -> ScoreSemantics:
        return "higher_is_better"

    def add(
        self,
        documents: list[str],
        embeddings: list[list[float]],
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        import uuid

        from qdrant_client.models import PointStruct

        _validate_add_inputs(
            documents=documents,
            embeddings=embeddings,
            metadata=metadata,
            context="QdrantVectorStore.add",
        )
        _ensure_fixed_dimension(embeddings, self._dimension, context="QdrantVectorStore.add")
        metas = metadata or [{} for _ in documents]
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=emb,
                payload={"_document": doc, **meta},
            )
            for doc, emb, meta in zip(documents, embeddings, metas, strict=False)
        ]
        self._client.upsert(collection_name=self._collection, points=points)
        return [str(p.id) for p in points]

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        if len(query_embedding) != self._dimension:
            msg = (
                "QdrantVectorStore.search: query embedding dimension mismatch, "
                f"expected {self._dimension}, got {len(query_embedding)}"
            )
            raise ValueError(msg)

        qfilter = None
        if metadata_filters:
            qfilter = Filter(
                must=[FieldCondition(key=k, match=MatchValue(value=v)) for k, v in metadata_filters.items()]
            )

        hits = self._client.search(
            collection_name=self._collection,
            query_vector=query_embedding,
            limit=top_k,
            query_filter=qfilter,
        )

        results: list[SearchResult] = []
        for hit in hits:
            raw_score = float(hit.score)
            score = _relevance_from_distance(raw_score) if self._metric == "l2" else raw_score
            results.append(
                SearchResult(
                    id=str(hit.id),
                    document=hit.payload.get("_document", "") if hit.payload else "",
                    score=score,
                    metadata={
                        **{k: v for k, v in (hit.payload or {}).items() if k != "_document"},
                        "_raw_score": raw_score,
                        "_score_metric": self._metric,
                        "_score_semantics": "higher_is_better",
                    },
                )
            )
        return results

    def delete(self, ids: list[str]) -> None:
        from qdrant_client.models import PointIdsList

        self._client.delete(
            collection_name=self._collection,
            points_selector=PointIdsList(points=ids),
        )

    def close(self) -> None:
        self._client.close()


class PineconeVectorStore(VectorStore):
    """
    Pinecone-backed vector store (requires ``pip install pinecone-client``).

    Concurrency is delegated to the Pinecone client library.

    .. note::

        The metric of a Pinecone index is set externally at index creation time
        and is **not** known to this wrapper.  ``score_semantics`` therefore
        defaults to ``"unknown"``.  Applying a ``score_threshold`` will be
        skipped with a warning when the semantics are unknown.
    """

    def __init__(
        self,
        index_name: str,
        api_key: str = "",
        namespace: str = "",
        dimension: int | None = None,
    ) -> None:
        try:
            from pinecone import Pinecone
        except ImportError as e:
            msg = "pinecone required: pip install pinecone-client"
            raise ImportError(msg) from e

        self._pc = Pinecone(api_key=api_key)
        self._index = self._pc.Index(index_name)
        self._namespace = namespace
        self._dimension = dimension

    @property
    def score_semantics(self) -> ScoreSemantics:
        return "unknown"

    def add(
        self,
        documents: list[str],
        embeddings: list[list[float]],
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        import uuid

        _validate_add_inputs(
            documents=documents,
            embeddings=embeddings,
            metadata=metadata,
            context="PineconeVectorStore.add",
        )
        if embeddings:
            inferred_dim = len(embeddings[0])
            _ensure_fixed_dimension(embeddings, inferred_dim, context="PineconeVectorStore.add")
            if self._dimension is None:
                self._dimension = inferred_dim
            elif self._dimension != inferred_dim:
                msg = (
                    "PineconeVectorStore.add: embedding dimension mismatch, "
                    f"expected {self._dimension}, got {inferred_dim}"
                )
                raise ValueError(msg)

        metas = metadata or [{} for _ in documents]
        vectors = []
        new_ids: list[str] = []
        for doc, emb, meta in zip(documents, embeddings, metas, strict=False):
            vid = str(uuid.uuid4())
            new_ids.append(vid)
            vectors.append(
                {
                    "id": vid,
                    "values": emb,
                    "metadata": {"_document": doc, **meta},
                }
            )
        self._index.upsert(vectors=vectors, namespace=self._namespace)
        return new_ids

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if self._dimension is not None and len(query_embedding) != self._dimension:
            msg = (
                "PineconeVectorStore.search: query embedding dimension mismatch, "
                f"expected {self._dimension}, got {len(query_embedding)}"
            )
            raise ValueError(msg)

        resp = self._index.query(
            vector=query_embedding,
            top_k=top_k,
            include_metadata=True,
            filter=metadata_filters or None,
            namespace=self._namespace,
        )

        return [
            SearchResult(
                id=m["id"],
                document=(m.get("metadata") or {}).get("_document", ""),
                score=float(m["score"]),
                metadata={
                    **{k: v for k, v in (m.get("metadata") or {}).items() if k != "_document"},
                    "_raw_score": float(m["score"]),
                    "_score_metric": "unknown",
                    "_score_semantics": "unknown",
                },
            )
            for m in resp.get("matches", [])
        ]

    def delete(self, ids: list[str]) -> None:
        self._index.delete(ids=ids, namespace=self._namespace)

    def close(self) -> None:
        """No-op — the Pinecone client does not hold persistent connections."""


class MilvusVectorStore(VectorStore):
    """
    **EXPERIMENTAL** — Milvus-backed vector store (requires ``pip install pymilvus``).

    Concurrency is delegated to the Milvus client library.

    Known limitations:

    * Metadata is stored as a JSON string column (``metadata_json``).
      Filtering uses ``LIKE`` expressions which are fragile, slow, and may
      produce false positives for complex or nested metadata.
    * The metric type must be specified at construction; incorrect values
      will lead to wrong score interpretation.
    """

    def __init__(
        self,
        collection_name: str,
        uri: str = "http://localhost:19530",
        dimension: int = 384,
        metric: Literal["cosine", "dot", "l2"] = "cosine",
    ) -> None:
        try:
            from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient
        except ImportError as e:
            msg = "pymilvus required: pip install pymilvus"
            raise ImportError(msg) from e

        warnings.warn(
            "MilvusVectorStore is experimental: metadata filtering uses JSON LIKE "
            "expressions which are fragile and may produce false positives.",
            stacklevel=2,
        )

        self._client = MilvusClient(uri=uri)
        self._collection = collection_name
        self._dimension = dimension
        self._metric = metric

        if not self._client.has_collection(collection_name):
            schema = CollectionSchema(
                fields=[
                    FieldSchema("id", DataType.VARCHAR, is_primary=True, max_length=64),
                    FieldSchema("document", DataType.VARCHAR, max_length=65535),
                    FieldSchema("metadata_json", DataType.VARCHAR, max_length=65535),
                    FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=dimension),
                ],
                enable_dynamic_field=True,
            )
            self._client.create_collection(
                collection_name=collection_name,
                schema=schema,
            )

    @property
    def score_semantics(self) -> ScoreSemantics:
        return "higher_is_better"

    def add(
        self,
        documents: list[str],
        embeddings: list[list[float]],
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        import uuid

        _validate_add_inputs(
            documents=documents,
            embeddings=embeddings,
            metadata=metadata,
            context="MilvusVectorStore.add",
        )
        _ensure_fixed_dimension(embeddings, self._dimension, context="MilvusVectorStore.add")
        metas = metadata or [{} for _ in documents]
        new_ids = [str(uuid.uuid4()) for _ in documents]
        rows = [
            {
                "id": vid,
                "document": doc,
                "metadata_json": json.dumps(meta, ensure_ascii=False),
                "embedding": emb,
            }
            for vid, doc, emb, meta in zip(new_ids, documents, embeddings, metas, strict=False)
        ]
        self._client.insert(collection_name=self._collection, data=rows)
        return new_ids

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if len(query_embedding) != self._dimension:
            msg = (
                "MilvusVectorStore.search: query embedding dimension mismatch, "
                f"expected {self._dimension}, got {len(query_embedding)}"
            )
            raise ValueError(msg)

        filter_expr = ""
        if metadata_filters:
            clauses = []
            for k, v in metadata_filters.items():
                if isinstance(v, str):
                    clauses.append(f'metadata_json like \'%"{k}": "{v}"%\'')
                else:
                    clauses.append(f"metadata_json like '%\"{k}\": {json.dumps(v)}%'")
            filter_expr = " and ".join(clauses)

        hits = self._client.search(
            collection_name=self._collection,
            data=[query_embedding],
            limit=top_k,
            output_fields=["document", "metadata_json"],
            filter=filter_expr,
        )

        results: list[SearchResult] = []
        for hit_list in hits:
            for hit in hit_list:
                entity = hit.get("entity", {})
                raw_meta = entity.get("metadata_json", "{}")
                try:
                    meta: dict[str, Any] = json.loads(raw_meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}

                raw_distance = float(hit["distance"])
                score = raw_distance if self._metric in ("cosine", "dot") else _relevance_from_distance(raw_distance)

                results.append(
                    SearchResult(
                        id=str(hit["id"]),
                        document=entity.get("document", ""),
                        score=score,
                        metadata={
                            **meta,
                            "_raw_score": raw_distance,
                            "_score_metric": self._metric,
                            "_score_semantics": "higher_is_better",
                        },
                    )
                )
        return results[:top_k]

    def delete(self, ids: list[str]) -> None:
        escaped = ", ".join(f'"{doc_id}"' for doc_id in ids)
        filter_expr = f"id in [{escaped}]"
        self._client.delete(
            collection_name=self._collection,
            filter=filter_expr,
        )

    def close(self) -> None:
        self._client.close()


_REMOTE_REQUIRED_PARAMS: dict[str, tuple[str, str]] = {
    "qdrant": (
        "collection_name",
        "QdrantVectorStore requires 'collection_name'. Pass it via store kwargs or environment config.",
    ),
    "pinecone": (
        "index_name",
        "PineconeVectorStore requires 'index_name'. Pass it via store kwargs or environment config.",
    ),
    "milvus": (
        "collection_name",
        "MilvusVectorStore requires 'collection_name'. Pass it via store kwargs or environment config.",
    ),
}


def _create_vector_store(store_type: str = "in_memory", **kwargs: Any) -> VectorStore:
    """Factory: create a VectorStore by backend name."""
    store_type = store_type.lower()

    required = _REMOTE_REQUIRED_PARAMS.get(store_type)
    if required is not None:
        param_name, error_msg = required
        if param_name not in kwargs or not kwargs[param_name]:
            raise ValueError(error_msg)

    if store_type == "in_memory":
        return InMemoryVectorStore(**kwargs)
    if store_type == "faiss":
        return FaissVectorStore(**kwargs)
    if store_type == "qdrant":
        return QdrantVectorStore(**kwargs)
    if store_type == "pinecone":
        return PineconeVectorStore(**kwargs)
    if store_type == "milvus":
        return MilvusVectorStore(**kwargs)
    msg = f"Unknown vector store type: {store_type}"
    raise ValueError(msg)


def _index_texts(
    *,
    texts: list[str],
    metadata: list[dict[str, Any]] | None,
    chunker: DocumentChunker,
    provider: EmbeddingProvider,
    store: VectorStore,
    batch_size: int = 500,
) -> list[str]:
    """Chunk, embed and persist texts in a vector store (batched)."""

    def _with_default(meta: dict[str, Any], key: str, value: Any) -> None:
        if key not in meta or meta.get(key) is None:
            meta[key] = value

    if metadata is not None and len(metadata) != len(texts):
        msg = f"_index_texts: metadata/documents length mismatch, metadata={len(metadata)}, documents={len(texts)}"
        raise ValueError(msg)

    all_chunks: list[str] = []
    all_meta: list[dict[str, Any]] = []

    for i, text in enumerate(texts):
        chunks = chunker.chunk(text)
        meta = dict(metadata[i]) if metadata and i < len(metadata) else {}
        _with_default(meta, "_document_index", i)
        _with_default(meta, "doc_id", f"doc_{i}")
        _with_default(meta, "source", "unknown")
        _with_default(meta, "title", f"Document {i}")

        for j, chunk in enumerate(chunks):
            all_chunks.append(chunk)
            chunk_meta = dict(meta)
            chunk_meta["_chunk_index"] = j
            all_meta.append(chunk_meta)

    if not all_chunks:
        return []

    all_ids: list[str] = []
    effective_batch = max(1, batch_size)

    for start in range(0, len(all_chunks), effective_batch):
        batch_chunks = all_chunks[start : start + effective_batch]
        batch_meta = all_meta[start : start + effective_batch]

        embeddings = provider.embed_batch(batch_chunks)
        if not embeddings:
            continue

        inferred_dim = len(embeddings[0])
        _ensure_fixed_dimension(embeddings, inferred_dim, context="_index_texts")
        expected_dim = getattr(store, "_dimension", None)
        if expected_dim is not None:
            _ensure_fixed_dimension(embeddings, int(expected_dim), context="_index_texts")

        ids = store.add(batch_chunks, embeddings, batch_meta)
        all_ids.extend(ids)

    return all_ids


def _split_store_kwargs(overrides: dict[str, Any], known_keys: set[str]) -> dict[str, Any]:
    return {k: v for k, v in overrides.items() if k not in known_keys}


def _component_config_fingerprint() -> str:
    """Build a fingerprint covering provider and chunker configuration."""
    from gmas.config.settings import FrameworkSettings

    try:
        settings = FrameworkSettings()
    except (ValidationError, OSError):
        return "defaults"

    parts = {
        "embedding_model": settings.embedding_model,
        "embedding_normalize": settings.embedding_normalize,
        "embedding_batch_size": settings.embedding_batch_size,
    }
    return json.dumps(parts, sort_keys=True, default=str)


def _shared_components(
    *,
    shared_key: str,
    store_type: str,
    store_kwargs: dict[str, Any] | None = None,
) -> tuple["VectorStore", "EmbeddingProvider", "DocumentChunker"]:
    kwargs = store_kwargs or {}
    config_fp = _component_config_fingerprint()
    full_key = f"{shared_key}::{store_type}::{json.dumps(kwargs, sort_keys=True, default=str)}::{config_fp}"

    with _shared_components_lock:
        cached = _SHARED_VECTOR_COMPONENTS.get(full_key)
        if cached is not None:
            return cached

        store = _create_vector_store(store_type, **kwargs)
        provider = SentenceTransformerProvider.from_settings()
        chunker = DocumentChunker()
        trio = (store, provider, chunker)
        _SHARED_VECTOR_COMPONENTS[full_key] = trio
        return trio


class VectorSearchTool(BaseTool):
    """
    Tool for semantic similarity search over a document knowledge base.

    Supports semantic retrieval with optional citation formatting
    and context-guardrail mode for RAG pipelines.

    Example:
        # Basic usage — pair index and search tools on shared store
        search = VectorSearchTool()
        index = VectorIndexTool(
            store=search._store,
            provider=search._provider,
            chunker=search._chunker,
        )
        index.index(["Python is a programming language.", "Rust is fast."])
        result = search.execute(query="What is Python?")
        print(result.output)

        # With metadata and filtering
        index.index(
            texts=["Legal compliance report", "Marketing plan Q3"],
            metadata=[{"dept": "legal"}, {"dept": "marketing"}],
        )
        result = search.execute(query="compliance", filters={"dept": "legal"})

        # FAISS backend
        search = VectorSearchTool(store_type="faiss", dimension=384)

        # Configure from environment (GMAS_VECTOR_* variables)
        search = VectorSearchTool.from_settings(shared=False)

        # Context guardrail mode for RAG (prompt-level instruction only)
        search = VectorSearchTool(context_guardrail=True, citation_mode="numbered")

    """

    def __init__(
        self,
        store: VectorStore | None = None,
        provider: EmbeddingProvider | None = None,
        chunker: DocumentChunker | None = None,
        top_k: int = 5,
        score_threshold: float = 0.0,
        max_context_tokens: int = 4096,
        context_template: str = "{context}",
        context_guardrail: bool = False,
        citation_mode: Literal["none", "inline", "numbered"] = "numbered",
        return_mode: Literal["context", "json"] = "context",
        store_type: str = "in_memory",
        # Deprecated alias — use context_guardrail instead
        strict_context_mode: bool | None = None,
        **store_kwargs: Any,
    ) -> None:
        self._owns_store = store is None
        self._store = store or _create_vector_store(store_type, **store_kwargs)
        self._provider = provider or SentenceTransformerProvider.from_settings()
        self._chunker = chunker or DocumentChunker()
        self._top_k = top_k
        self._score_threshold = score_threshold
        self._max_context_tokens = max_context_tokens
        self._context_template = context_template
        self._citation_mode = citation_mode
        self._return_mode = return_mode

        if strict_context_mode is not None:
            warnings.warn(
                "strict_context_mode is deprecated; use context_guardrail instead",
                DeprecationWarning,
                stacklevel=2,
            )
            self._context_guardrail = bool(strict_context_mode)
        else:
            self._context_guardrail = context_guardrail

    @classmethod
    def from_settings(cls, **overrides: Any) -> "VectorSearchTool":
        """Create tool configured from environment variables (GMAS_VECTOR_*)."""
        shared = bool(overrides.pop("shared", False))
        shared_key = overrides.pop("shared_key", None)
        known_keys = {
            "store",
            "provider",
            "chunker",
            "top_k",
            "score_threshold",
            "max_context_tokens",
            "context_template",
            "strict_context_mode",
            "context_guardrail",
            "citation_mode",
            "return_mode",
            "store_type",
        }
        store_kwargs = _split_store_kwargs(overrides, known_keys)
        defaults = _resolve_vector_search_defaults()

        store_type = str(overrides.get("store_type", defaults["store_type"]))
        store, provider, chunker = _normalize_components(
            overrides=overrides,
            shared=shared,
            shared_key=shared_key,
            store_type=store_type,
            store_kwargs=store_kwargs,
        )

        try:
            top_k = max(1, int(overrides.get("top_k", defaults["top_k"])))
        except (TypeError, ValueError):
            top_k = int(defaults["top_k"])
        try:
            score_threshold = max(0.0, float(overrides.get("score_threshold", defaults["score_threshold"])))
        except (TypeError, ValueError):
            score_threshold = float(defaults["score_threshold"])
        try:
            max_context_tokens = max(0, int(overrides.get("max_context_tokens", defaults["max_context_tokens"])))
        except (TypeError, ValueError):
            max_context_tokens = int(defaults["max_context_tokens"])
        context_template = str(overrides.get("context_template", defaults["context_template"]))
        citation_mode = _coerce_citation_mode(overrides.get("citation_mode", defaults["citation_mode"]))
        _return_mode_raw = str(overrides.get("return_mode", "context"))
        return_mode_val: Literal["context", "json"] = "json" if _return_mode_raw == "json" else "context"

        guardrail = overrides.get("context_guardrail")
        strict = overrides.get("strict_context_mode")
        if guardrail is not None:
            context_guardrail = bool(guardrail)
        elif strict is not None:
            context_guardrail = bool(strict)
        else:
            context_guardrail = bool(defaults["strict_context_mode"])

        return cls(
            store=store,
            provider=provider,
            chunker=chunker,
            top_k=top_k,
            score_threshold=score_threshold,
            max_context_tokens=max_context_tokens,
            context_template=context_template,
            context_guardrail=context_guardrail,
            citation_mode=citation_mode,
            return_mode=return_mode_val,
            store_type=store_type,
            **store_kwargs,
        )

    @property
    def name(self) -> str:
        return "vector_search"

    @property
    def description(self) -> str:
        return (
            "Search a vector knowledge base by semantic similarity. "
            "Use 'query' to find relevant documents. "
            "Returns ranked results with text and relevance scores."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query to find relevant documents",
                },
                "top_k": {
                    "type": "integer",
                    "description": f"Number of results to return. Default: {self._top_k}",
                },
                "score_threshold": {
                    "type": "number",
                    "description": (
                        f"Minimum score to include. Note: score scale is backend-specific "
                        f"and not directly portable across different store types. Default: {self._score_threshold}"
                    ),
                },
                "filters": {
                    "type": "object",
                    "description": 'Metadata filters to narrow results (e.g. {"source": "wiki"})',
                },
                "return_mode": {
                    "type": "string",
                    "enum": ["context", "json"],
                    "description": f'Output format. Default: "{self._return_mode}"',
                },
            },
            "required": ["query"],
        }

    @property
    def store(self) -> VectorStore:
        return self._store

    @property
    def provider(self) -> EmbeddingProvider:
        return self._provider

    @property
    def chunker(self) -> DocumentChunker:
        return self._chunker

    @property
    def owns_store(self) -> bool:
        return self._owns_store

    def format_context(self, results: list[SearchResult], query: str = "") -> str:
        """Format search results with optional citations."""
        if not results:
            return "No relevant documents found."

        if self._citation_mode == "none":
            parts = [r.document for r in results]
        elif self._citation_mode == "inline":
            parts = []
            for r in results:
                meta_str = ", ".join(f"{k}: {v}" for k, v in r.metadata.items() if not k.startswith("_"))
                citation = f" [{meta_str}]" if meta_str else ""
                parts.append(f"{r.document}{citation}")
        else:
            parts = [f"[{i}] (score: {r.score:.4f}) {r.document}" for i, r in enumerate(results, 1)]

        context = "\n\n".join(parts)

        if self._context_guardrail:
            context = f"Answer only based on the context below:\n\n{context}"

        try:
            return self._context_template.format(context=context, query=query)
        except KeyError:
            return context

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Upper-bound token estimate: len(text) / 4 (approx. for GPT models)."""
        return max(1, len(text) // _CHARS_PER_TOKEN)

    def _truncate_chunk(self, text: str, max_tokens: int) -> str:
        """Return a prefix of *text* that fits within *max_tokens*."""
        max_chars = max(1, max_tokens * _CHARS_PER_TOKEN)
        return text[:max_chars]

    def _apply_score_threshold(
        self, results: list[SearchResult], score_threshold: float
    ) -> tuple[list[SearchResult], str | None, str | None]:
        """
        Apply score threshold filtering to results.

        Returns:
            Tuple of (filtered_results, score_warning, score_scale_note)

        """
        score_warning: str | None = None
        score_scale_note: str | None = None

        if score_threshold <= 0:
            return results, score_warning, score_scale_note

        if self._store.score_semantics == "unknown":
            score_warning = (
                "score_threshold ignored: the store's score semantics are unknown "
                "(scores may not be comparable across backends)"
            )
            logger.warning(score_warning)
            return results, score_warning, score_scale_note

        filtered = [r for r in results if r.score >= score_threshold]
        if self._store.score_semantics == "higher_is_better":
            score_scale_note = (
                "score scale is backend- and metric-specific; "
                "threshold values are not directly portable across store types"
            )
        return filtered, score_warning, score_scale_note

    def _apply_token_budget(self, results: list[SearchResult]) -> list[SearchResult]:
        """Truncate results to fit within token budget."""
        if self._max_context_tokens <= 0:
            return results

        overhead = 0
        if self._context_guardrail:
            overhead += self._estimate_tokens("Answer only based on the context below:\n\n")
        template_overhead = self._estimate_tokens(
            self._context_template.replace("{context}", "").replace("{query}", "")
        )
        overhead += template_overhead
        budget = max(1, self._max_context_tokens - overhead)

        if self._citation_mode == "numbered":
            per_result_overhead = self._estimate_tokens("[00] (score: 0.0000) ")
        elif self._citation_mode == "inline":
            per_result_overhead = self._estimate_tokens(" [metadata: value]")
        else:
            per_result_overhead = 0
        separator_overhead = self._estimate_tokens("\n\n")

        truncated: list[SearchResult] = []
        total_tokens = 0
        for r in results:
            est = self._estimate_tokens(r.document) + per_result_overhead + separator_overhead
            if total_tokens + est > budget:
                if not truncated:
                    remaining = budget - total_tokens - per_result_overhead
                    trimmed_doc = self._truncate_chunk(r.document, max(1, remaining))
                    truncated.append(
                        SearchResult(
                            id=r.id,
                            document=trimmed_doc,
                            score=r.score,
                            metadata=r.metadata,
                        )
                    )
                break
            truncated.append(r)
            total_tokens += est
        return truncated

    def _build_output(
        self,
        results: list[SearchResult],
        query: str,
        return_mode: str,
        score_warning: str | None,
        score_scale_note: str | None,
    ) -> str:
        """Build output string from results."""
        if score_scale_note and results:
            results[0].metadata["_score_scale_note"] = score_scale_note

        context_str = self.format_context(results, query)

        if return_mode == "json":
            payload: dict[str, Any] = {
                "results": [
                    {
                        "id": r.id,
                        "document": r.document,
                        "score": r.score,
                        "metadata": r.metadata,
                    }
                    for r in results
                ],
                "query": query,
                "context": context_str,
            }
            if score_warning:
                payload["warning"] = score_warning
            return json.dumps(payload, ensure_ascii=False)

        output = context_str
        if score_warning:
            output = f"[WARNING: {score_warning}]\n\n{output}"
        return output

    def execute(self, **kwargs: Any) -> "ToolResult":
        from .base import ToolResult

        query = kwargs.get("query", "")
        if not query:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="No query provided",
            )

        raw_top_k = kwargs.get("top_k", self._top_k)
        try:
            top_k = max(1, int(raw_top_k))
        except (TypeError, ValueError):
            top_k = self._top_k

        raw_threshold = kwargs.get("score_threshold", self._score_threshold)
        try:
            score_threshold = max(0.0, float(raw_threshold))
        except (TypeError, ValueError):
            score_threshold = self._score_threshold

        filters = kwargs.get("filters")
        if filters is not None and not isinstance(filters, dict):
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="filters must be a dict or None",
            )

        return_mode = kwargs.get("return_mode", self._return_mode)
        if return_mode not in ("context", "json"):
            return_mode = self._return_mode

        try:
            query_embedding = self._provider.embed(query)
            results = self._store.search(
                query_embedding=query_embedding,
                top_k=top_k,
                metadata_filters=filters,
            )

            results, score_warning, score_scale_note = self._apply_score_threshold(results, score_threshold)
            results = self._apply_token_budget(results)
            output = self._build_output(results, query, return_mode, score_warning, score_scale_note)

            return ToolResult(
                tool_name=self.name,
                success=True,
                output=output,
            )

        except (ValueError, TypeError, RuntimeError, OSError) as e:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Search failed: {e}",
            )

    async def execute_async(self, **kwargs: Any) -> "ToolResult":
        """Async version of ``execute`` that runs in a worker thread."""
        return await asyncio.to_thread(self.execute, **kwargs)

    def close(self) -> None:
        """
        Close the underlying store if this tool owns it.

        Stores that were injected externally or obtained from the shared
        cache are **not** closed — only stores created internally by this
        tool instance are eligible for cleanup.
        """
        if self._owns_store:
            self._store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def _strict_context_mode(self) -> bool:
        """Deprecated alias for ``_context_guardrail``."""
        return self._context_guardrail

    @_strict_context_mode.setter
    def _strict_context_mode(self, value: bool) -> None:
        self._context_guardrail = value


class VectorIndexTool(BaseTool):
    """Tool for indexing/deleting documents in a vector store."""

    def __init__(
        self,
        store: VectorStore | None = None,
        provider: EmbeddingProvider | None = None,
        chunker: DocumentChunker | None = None,
        store_type: str = "in_memory",
        **store_kwargs: Any,
    ) -> None:
        self._owns_store = store is None
        self._store = store or _create_vector_store(store_type, **store_kwargs)
        self._provider = provider or SentenceTransformerProvider.from_settings()
        self._chunker = chunker or DocumentChunker()

    @classmethod
    def from_settings(cls, **overrides: Any) -> "VectorIndexTool":
        """Create tool configured from environment variables (GMAS_VECTOR_*)."""
        shared = bool(overrides.pop("shared", False))
        shared_key = overrides.pop("shared_key", None)
        known_keys = {"store", "provider", "chunker", "store_type"}
        store_kwargs = _split_store_kwargs(overrides, known_keys)
        default_store_type = _resolve_vector_index_default_store_type()
        store_type = str(overrides.get("store_type", default_store_type))
        store, provider, chunker = _normalize_components(
            overrides=overrides,
            shared=shared,
            shared_key=shared_key,
            store_type=store_type,
            store_kwargs=store_kwargs,
        )

        return cls(
            store=store,
            provider=provider,
            chunker=chunker,
            store_type=store_type,
            **store_kwargs,
        )

    @property
    def name(self) -> str:
        return "vector_index"

    @property
    def description(self) -> str:
        return (
            "Index documents into a vector knowledge base. "
            "Supports index operation for adding text chunks and delete operation by vector ids."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["index", "delete"],
                    "description": "Operation type. Default: index",
                },
                "texts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Documents to index (used when operation=index).",
                },
                "metadata": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional per-document metadata for texts.",
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Vector ids to delete (used when operation=delete).",
                },
            },
        }

    def index(
        self,
        texts: list[str],
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        return _index_texts(
            texts=texts,
            metadata=metadata,
            chunker=self._chunker,
            provider=self._provider,
            store=self._store,
        )

    def delete(self, ids: list[str]) -> None:
        self._store.delete(ids)

    def execute(self, **kwargs: Any) -> "ToolResult":
        from .base import ToolResult

        operation = str(kwargs.get("operation", "index")).strip().lower()
        try:
            if operation == "delete":
                ids = kwargs.get("ids", [])
                if not isinstance(ids, list) or not ids:
                    return ToolResult(tool_name=self.name, success=False, error="No ids provided for delete")
                self.delete([str(i) for i in ids])
                return ToolResult(tool_name=self.name, success=True, output=f"Deleted {len(ids)} vectors.")

            texts = kwargs.get("texts", kwargs.get("documents", []))
            metadata_raw = kwargs.get("metadata")
            if isinstance(texts, str):
                texts = [texts]
            if not isinstance(texts, list) or not texts:
                return ToolResult(tool_name=self.name, success=False, error="No texts provided for index")

            text_items = [str(t) for t in texts]

            if metadata_raw is not None:
                if not isinstance(metadata_raw, list):
                    return ToolResult(tool_name=self.name, success=False, error="metadata must be a list of objects")
                for idx, entry in enumerate(metadata_raw):
                    if not isinstance(entry, dict):
                        return ToolResult(
                            tool_name=self.name,
                            success=False,
                            error=f"metadata[{idx}] must be a dict, got {type(entry).__name__}",
                        )

            meta_items = metadata_raw if isinstance(metadata_raw, list) else None

            ids = self.index(text_items, meta_items)
            payload = {
                "message": f"Indexed {len(ids)} chunks from {len(text_items)} documents.",
                "indexed_chunks": len(ids),
                "documents": len(text_items),
                "ids": ids,
            }
            return ToolResult(
                tool_name=self.name,
                success=True,
                output=json.dumps(payload, ensure_ascii=False),
            )
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            return ToolResult(tool_name=self.name, success=False, error=f"Indexing failed: {e}")

    def close(self) -> None:
        """
        Close the underlying store if this tool owns it.

        Stores that were injected externally or obtained from the shared
        cache are **not** closed — only stores created internally by this
        tool instance are eligible for cleanup.
        """
        if self._owns_store:
            self._store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def execute_async(self, **kwargs: Any) -> "ToolResult":
        """Async version of ``execute`` that runs in a worker thread."""
        return await asyncio.to_thread(self.execute, **kwargs)


def _create_vector_search_tool(**kwargs: Any) -> "VectorSearchTool":
    return VectorSearchTool(**kwargs)


def _create_vector_index_tool(**kwargs: Any) -> "VectorIndexTool":
    return VectorIndexTool(**kwargs)


register_tool_factory("vector_search", _create_vector_search_tool)
register_tool_factory("vector_index", _create_vector_index_tool)
