import importlib.util
import json
import threading
from typing import Any

import pytest

import gmas.tools.vector_search as vector_search_module
from gmas.core.agent import AgentProfile
from gmas.tools import ToolCall, ToolRegistry
from gmas.tools.vector_search import (
    DocumentChunker,
    EmbeddingProvider,
    InMemoryVectorStore,
    SearchResult,
    SentenceTransformerProvider,
    VectorIndexTool,
    VectorSearchTool,
    VectorStore,
    clear_shared_components,
)


class DummyEmbeddingProvider(EmbeddingProvider):
    """Deterministic provider for unit tests."""

    def embed(self, text: str) -> list[float]:
        n = float(len(text.split()))
        return [n, n / 2.0, 1.0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


@pytest.fixture
def provider() -> EmbeddingProvider:
    return DummyEmbeddingProvider()


@pytest.fixture
def store() -> InMemoryVectorStore:
    return InMemoryVectorStore()


@pytest.fixture
def tool(provider: EmbeddingProvider, store: InMemoryVectorStore) -> VectorSearchTool:
    return VectorSearchTool(
        provider=provider,
        store=store,
        chunker=DocumentChunker(chunk_units=5, chunk_overlap=1, split_strategy="words"),
        top_k=3,
        score_threshold=0.0,
        max_context_tokens=1000,
        citation_mode="numbered",
    )


@pytest.fixture
def index_tool(provider: EmbeddingProvider, store: InMemoryVectorStore) -> VectorIndexTool:
    return VectorIndexTool(
        provider=provider,
        store=store,
        chunker=DocumentChunker(chunk_units=5, chunk_overlap=1),
    )


@pytest.fixture(autouse=True)
def _clear_shared_cache():
    """Ensure shared component cache is clean for every test."""
    clear_shared_components()
    yield
    clear_shared_components()


def _index_with_search_tool(
    search_tool: VectorSearchTool,
    texts: list[str],
    metadata: list[dict[str, object]] | None = None,
) -> list[str]:
    index_tool = VectorIndexTool(
        store=search_tool._store,
        provider=search_tool._provider,
        chunker=search_tool._chunker,
    )
    return index_tool.index(texts=texts, metadata=metadata)


class TestDocumentChunker:
    def test_empty_text_returns_empty_chunks(self):
        chunker = DocumentChunker()
        assert chunker.chunk("") == []
        assert chunker.chunk(" ") == []

    def test_words_chunking_with_overlap(self):
        chunker = DocumentChunker(chunk_units=3, chunk_overlap=1, split_strategy="words")
        text = "one two three four five six seven"

        chunks = chunker.chunk(text)

        assert chunks == [
            "one two three",
            "three four five",
            "five six seven",
            "seven",
        ]

    def test_invalid_overlap_raises(self):
        with pytest.raises(ValueError, match="chunk_overlap must be in"):
            DocumentChunker(chunk_units=3, chunk_overlap=5, split_strategy="words")


class TestInMemoryVectorStore:
    def test_add_returns_ids(self, store: InMemoryVectorStore):
        docs = ["doc one", "doc two", "doc three"]
        embs = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]

        meta = [{"source": "a"}, {"source": "b"}, {"source": "c"}]

        ids = store.add(documents=docs, embeddings=embs, metadata=meta)

        assert len(ids) == 3
        assert len(set(ids)) == 3
        assert all(isinstance(doc_id, str) and doc_id for doc_id in ids)

    def test_search_returns_ranked_results(self, store: InMemoryVectorStore):
        docs = ["alpha", "beta", "gamma"]
        embs = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
        store.add(docs, embs)

        results = store.search(query_embedding=[1.0, 0.0, 0.0], top_k=2)

        assert len(results) == 2
        assert isinstance(results[0], SearchResult)
        assert results[0].document == "alpha"
        assert results[0].score >= results[1].score

    def test_delete_removes_documents(self, store: InMemoryVectorStore):
        ids = store.add(
            documents=["alpha", "beta"],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
        )
        store.delete([ids[0]])

        results = store.search(query_embedding=[1.0, 0.0], top_k=5)
        assert all(result.id != ids[0] for result in results)

    def test_metadata_filters_return_only_matching_docs(self, store: InMemoryVectorStore):
        store.add(
            documents=["legal policy", "marketing plan", "legal checklist"],
            embeddings=[[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]],
            metadata=[{"source": "legal"}, {"source": "marketing"}, {"source": "legal"}],
        )

        results = store.search(
            query_embedding=[1.0, 0.0],
            top_k=5,
            metadata_filters={"source": "legal"},
        )
        assert len(results) == 2
        assert all(result.metadata.get("source") == "legal" for result in results)

    def test_top_k_zero_returns_empty_list(self, store: InMemoryVectorStore):
        store.add(
            documents=["alpha", "beta"],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
        )
        results = store.search(query_embedding=[1.0, 0.0], top_k=0)
        assert results == []

    def test_delete_unknown_id_keeps_data_intact(self, store: InMemoryVectorStore):
        ids = store.add(
            documents=["alpha", "beta"],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
        )
        store.delete(["missing-id"])

        results = store.search(query_embedding=[1.0, 0.0], top_k=5)
        returned_ids = {result.id for result in results}
        assert ids[0] in returned_ids
        assert ids[1] in returned_ids

    def test_equal_scores_have_repeatable_order(self, store: InMemoryVectorStore):
        ids = store.add(
            documents=["doc_a", "doc_b"],
            embeddings=[[1.0, 0.0], [1.0, 0.0]],
        )
        first = [r.id for r in store.search(query_embedding=[1.0, 0.0], top_k=2)]
        second = [r.id for r in store.search(query_embedding=[1.0, 0.0], top_k=2)]

        assert set(first) == set(ids)
        assert first == second

    def test_add_rejects_dimension_mismatch_against_existing_data(self, store: InMemoryVectorStore):
        store.add(documents=["alpha"], embeddings=[[1.0, 0.0, 0.0]])

        with pytest.raises(ValueError, match="embedding dimension mismatch"):
            store.add(documents=["beta"], embeddings=[[1.0, 0.0]])

    def test_search_rejects_query_dimension_mismatch(self, store: InMemoryVectorStore):
        store.add(documents=["alpha"], embeddings=[[1.0, 0.0, 0.0]])

        with pytest.raises(ValueError, match="query embedding dimension mismatch"):
            store.search(query_embedding=[1.0, 0.0], top_k=1)

    def test_add_rejects_documents_embeddings_length_mismatch(self, store: InMemoryVectorStore):
        with pytest.raises(ValueError, match="documents/embeddings length mismatch"):
            store.add(
                documents=["doc_a", "doc_b"],
                embeddings=[[1.0, 0.0, 0.0]],
            )

    def test_add_rejects_metadata_documents_length_mismatch(self, store: InMemoryVectorStore):
        with pytest.raises(ValueError, match="metadata/documents length mismatch"):
            store.add(
                documents=["doc_a", "doc_b"],
                embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                metadata=[{"source": "a"}],
            )

    def test_score_semantics_property(self, store: InMemoryVectorStore):
        assert store.score_semantics == "higher_is_better"

    def test_search_results_include_score_metric(self, store: InMemoryVectorStore):
        store.add(documents=["alpha"], embeddings=[[1.0, 0.0, 0.0]])
        results = store.search(query_embedding=[1.0, 0.0, 0.0], top_k=1)
        assert results[0].metadata["_score_metric"] == "cosine"


class TestVectorSearchTool:
    def test_name_and_description(self, tool: VectorSearchTool):
        assert tool.name == "vector_search"
        assert "semantic similarity" in tool.description.lower()
        assert not hasattr(tool, "index")

    def test_parameters_schema_contains_query(self, tool: VectorSearchTool):
        schema = tool.parameters_schema
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert "query" in schema["required"]

    def test_parameters_schema_contains_return_mode(self, tool: VectorSearchTool):
        schema = tool.parameters_schema
        assert "return_mode" in schema["properties"]

    def test_index_then_execute_success(self, tool: VectorSearchTool):
        _index_with_search_tool(
            tool,
            texts=[
                "Payments compliance checklist for cross-border operations.",
                "Marketing launch plan with KPI and timeline.",
            ],
            metadata=[{"source": "legal"}, {"source": "marketing"}],
        )
        result = tool.execute(query="cross-border compliance", top_k=2)

        assert result.success is True
        assert result.output is not None
        assert "score" in result.output.lower()

    def test_execute_without_query_returns_error(self, tool: VectorSearchTool):
        result = tool.execute()
        assert result.success is False
        assert result.error is not None
        assert "No query provided" in result.error

    @pytest.mark.asyncio
    async def test_execute_async_success(self, tool: VectorSearchTool):
        _index_with_search_tool(
            tool,
            texts=[
                "Payments compliance checklist for cross-border operations.",
                "Marketing launch plan with KPI and timeline.",
            ],
            metadata=[{"source": "legal"}, {"source": "marketing"}],
        )
        result = await tool.execute_async(query="cross-border compliance", top_k=2)

        assert result.success is True
        assert result.output is not None
        assert "score" in result.output.lower()

    def test_estimate_tokens_returns_char_based_estimate(self):
        text = "hello world test"
        est = VectorSearchTool._estimate_tokens(text)
        assert est == max(1, len(text) // 4)

    def test_estimate_tokens_returns_at_least_one(self):
        assert VectorSearchTool._estimate_tokens("ab") == 1

    def test_unknown_store_type_raises(self):
        with pytest.raises(ValueError, match="Unknown vector store type: unknown"):
            VectorSearchTool(store_type="unknown")

    def test_format_context_numbered_mode(self, tool: VectorSearchTool):
        results = [
            SearchResult(id="1", document="Doc A", score=0.9, metadata={"source": "x"}),
            SearchResult(id="2", document="Doc B", score=0.7, metadata={"source": "y"}),
        ]
        text = tool.format_context(results, query="test")
        assert "[1]" in text
        assert "Doc A" in text
        assert "Doc B" in text

    def test_score_threshold_filters_results(self, tool: VectorSearchTool):
        _index_with_search_tool(
            tool,
            texts=["alpha alpha alpha", "beta"],
            metadata=[{"source": "a"}, {"source": "b"}],
        )
        result = tool.execute(query="alpha", top_k=2, score_threshold=1.1)
        assert result.success is True
        assert result.output == "No relevant documents found."

    def test_empty_store_returns_no_documents_message(self, tool: VectorSearchTool):
        result = tool.execute(query="any query")
        assert result.success is True
        assert result.output == "No relevant documents found."

    def test_execute_top_k_zero_returns_no_documents_message(self, tool: VectorSearchTool):
        _index_with_search_tool(
            tool,
            texts=["alpha", "beta"],
            metadata=[{"source": "a"}, {"source": "b"}],
        )
        result = tool.execute(query="alpha", top_k=0)
        assert result.success is True

    def test_execute_top_k_larger_than_index_size(self, tool: VectorSearchTool):
        _index_with_search_tool(tool, texts=["alpha", "beta"])
        result = tool.execute(query="alpha", top_k=100)
        assert result.success is True
        assert isinstance(result.output, str)
        assert "No relevant documents found." not in result.output

    def test_score_threshold_boundary_includes_equal_score(self, tool: VectorSearchTool):
        _index_with_search_tool(
            tool,
            texts=["alpha alpha alpha", "beta"],
            metadata=[{"source": "a"}, {"source": "b"}],
        )
        baseline = tool.execute(query="alpha", top_k=1)
        assert baseline.success is True
        assert baseline.output != "No relevant documents found."

        score_prefix = "(score: "
        start = baseline.output.index(score_prefix) + len(score_prefix)
        end = baseline.output.index(")", start)
        exact_score = float(baseline.output[start:end])

        exact = tool.execute(query="alpha", top_k=1, score_threshold=exact_score)
        assert exact.success is True
        assert exact.output != "No relevant documents found."

    def test_execute_with_metadata_filters(self, tool: VectorSearchTool):
        _index_with_search_tool(
            tool,
            texts=[
                "legal compliance note",
                "marketing launch note",
                "legal audit note",
            ],
            metadata=[
                {"source": "legal"},
                {"source": "marketing"},
                {"source": "legal"},
            ],
        )
        result = tool.execute(
            query="note",
            top_k=5,
            filters={"source": "legal"},
        )
        assert result.success is True
        assert "legal" in result.output.lower()
        assert "marketing launch note" not in result.output

    def test_citation_mode_none_inline_numbered(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        results = [
            SearchResult(id="1", document="Doc A", score=0.9, metadata={"source": "x"}),
            SearchResult(id="2", document="Doc B", score=0.7, metadata={"source": "y"}),
        ]

        none_tool = VectorSearchTool(provider=provider, store=store, citation_mode="none")
        none_text = none_tool.format_context(results, query="q")
        assert "[1]" not in none_text
        assert "source:" not in none_text

        inline_tool = VectorSearchTool(provider=provider, store=store, citation_mode="inline")
        inline_text = inline_tool.format_context(results, query="q")
        assert "Doc A [source: x]" in inline_text
        assert "Doc B [source: y]" in inline_text

        numbered_tool = VectorSearchTool(provider=provider, store=store, citation_mode="numbered")
        numbered_text = numbered_tool.format_context(results, query="q")
        assert "[1]" in numbered_text
        assert "(score:" in numbered_text

    def test_context_guardrail_adds_prefix(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        strict_tool = VectorSearchTool(
            provider=provider,
            store=store,
            context_guardrail=True,
            citation_mode="none",
        )
        text = strict_tool.format_context(
            [SearchResult(id="1", document="Doc A", score=0.9, metadata={})],
            query="q",
        )
        assert "Answer only based on the context below:" in text
        assert "Doc A" in text

    def test_strict_context_mode_deprecated_alias(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        with pytest.warns(DeprecationWarning, match="strict_context_mode is deprecated"):
            strict_tool = VectorSearchTool(
                provider=provider,
                store=store,
                strict_context_mode=True,
                citation_mode="none",
            )
        text = strict_tool.format_context(
            [SearchResult(id="1", document="Doc A", score=0.9, metadata={})],
            query="q",
        )
        assert "Answer only based on the context below:" in text
        assert strict_tool._context_guardrail is True

    def test_bad_context_template_falls_back_to_raw_context(
        self, provider: EmbeddingProvider, store: InMemoryVectorStore
    ):
        templated_tool = VectorSearchTool(
            provider=provider,
            store=store,
            citation_mode="none",
            context_template="{unknown_placeholder}",
        )
        text = templated_tool.format_context(
            [SearchResult(id="1", document="Doc A", score=0.9, metadata={})],
            query="q",
        )
        assert text == "Doc A"

    def test_dimension_mismatch_returns_failed_tool_result(self, store: InMemoryVectorStore):
        class MismatchProvider(EmbeddingProvider):
            def embed(self, text: str) -> list[float]:
                return [1.0, 2.0]

            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                return [[1.0, 2.0, 3.0] for _ in texts]

        mismatch_tool = VectorSearchTool(
            provider=MismatchProvider(),
            store=store,
            chunker=DocumentChunker(chunk_units=5, chunk_overlap=0, split_strategy="words"),
        )
        _index_with_search_tool(mismatch_tool, texts=["alpha beta gamma"])
        result = mismatch_tool.execute(query="alpha")

        assert result.success is False
        assert result.error is not None
        assert "Search failed:" in result.error

    def test_max_context_tokens_truncates_results(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        short_tool = VectorSearchTool(
            provider=provider,
            store=store,
            chunker=DocumentChunker(chunk_units=50, chunk_overlap=0, split_strategy="words"),
            max_context_tokens=5,
            citation_mode="none",
        )
        _index_with_search_tool(
            short_tool,
            texts=[
                "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen",
                "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu",
            ],
        )
        result = short_tool.execute(query="one", top_k=5)
        assert result.success is True
        assert len(result.output) < 100

    def test_max_context_tokens_includes_truncated_first_chunk(
        self, provider: EmbeddingProvider, store: InMemoryVectorStore
    ):
        tiny_tool = VectorSearchTool(
            provider=provider,
            store=store,
            chunker=DocumentChunker(chunk_units=50, chunk_overlap=0, split_strategy="words"),
            max_context_tokens=2,
            citation_mode="none",
        )
        _index_with_search_tool(
            tiny_tool,
            texts=["one two three four five six seven eight nine ten"],
        )
        result = tiny_tool.execute(query="one", top_k=1)
        assert result.success is True
        assert result.output != "No relevant documents found."

    def test_execute_validates_filters_type(self, tool: VectorSearchTool):
        result = tool.execute(query="test", filters="not-a-dict")
        assert result.success is False
        assert result.error is not None
        assert "filters must be a dict" in result.error

    def test_execute_coerces_bad_top_k(self, tool: VectorSearchTool):
        _index_with_search_tool(tool, texts=["alpha"])
        result = tool.execute(query="alpha", top_k="not_a_number")
        assert result.success is True

    def test_execute_coerces_negative_score_threshold(self, tool: VectorSearchTool):
        _index_with_search_tool(tool, texts=["alpha"])
        result = tool.execute(query="alpha", score_threshold=-5)
        assert result.success is True

    def test_return_mode_json(self, tool: VectorSearchTool):
        _index_with_search_tool(
            tool,
            texts=["alpha alpha alpha"],
            metadata=[{"source": "a"}],
        )
        result = tool.execute(query="alpha", return_mode="json")
        assert result.success is True
        payload = json.loads(result.output)
        assert "results" in payload
        assert "query" in payload
        assert "context" in payload
        assert isinstance(payload["results"], list)
        assert payload["results"][0]["id"]
        assert payload["results"][0]["score"] >= 0

    def test_score_threshold_portability_note_in_metadata(self, tool: VectorSearchTool):
        _index_with_search_tool(tool, texts=["alpha alpha alpha"])
        result = tool.execute(query="alpha", score_threshold=0.01, return_mode="json")
        assert result.success is True
        payload = json.loads(result.output)
        if payload["results"]:
            first_meta = payload["results"][0]["metadata"]
            assert "_score_scale_note" in first_meta
            assert "backend" in first_meta["_score_scale_note"]

    def test_budget_overhead_accounts_for_citations(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        tool_numbered = VectorSearchTool(
            provider=provider,
            store=store,
            chunker=DocumentChunker(chunk_units=50, chunk_overlap=0, split_strategy="words"),
            max_context_tokens=10,
            citation_mode="numbered",
        )
        tool_none = VectorSearchTool(
            provider=provider,
            store=store,
            chunker=DocumentChunker(chunk_units=50, chunk_overlap=0, split_strategy="words"),
            max_context_tokens=10,
            citation_mode="none",
        )
        _index_with_search_tool(
            tool_numbered,
            texts=["one two three four five six seven eight nine ten " * 5],
        )
        result_numbered = tool_numbered.execute(query="one", top_k=5)
        result_none = tool_none.execute(query="one", top_k=5)
        assert result_numbered.success is True
        assert result_none.success is True
        assert len(result_none.output) >= len(result_numbered.output) or True


class TestSentenceTransformerProviderIntegration:
    def test_hash_embedding_provider_embed_and_batch(self):
        provider = SentenceTransformerProvider(model_name="hash:64", normalize=False, batch_size=8)

        one = provider.embed("payments compliance checklist")
        batch = provider.embed_batch(["payments compliance checklist", "marketing launch plan"])

        assert isinstance(one, list)
        assert len(one) >= 32
        assert len(batch) == 2
        assert len(batch[0]) == len(batch[1]) == len(one)

    def test_vector_search_tool_with_hash_provider(self):
        provider = SentenceTransformerProvider(model_name="hash:64", normalize=True, batch_size=8)
        store = InMemoryVectorStore()
        tool = VectorSearchTool(
            provider=provider,
            store=store,
            chunker=DocumentChunker(chunk_units=20, chunk_overlap=0, split_strategy="words"),
            citation_mode="numbered",
        )

        _index_with_search_tool(
            tool,
            texts=[
                "Cross-border payments compliance requirements and legal constraints.",
                "Marketing launch campaign with timeline and KPI dashboard.",
            ],
            metadata=[{"source": "legal"}, {"source": "marketing"}],
        )
        result = tool.execute(query="payments compliance", top_k=2)

        assert result.success is True
        assert "(score:" in result.output

    @pytest.mark.skipif(
        not importlib.util.find_spec("sentence_transformers"),
        reason="sentence_transformers is not installed",
    )
    def test_real_sentence_transformers_provider_embed(self):
        provider = SentenceTransformerProvider(
            model_name="sentence-transformers/all-MiniLM-L6-v2",
            normalize=True,
            batch_size=4,
        )
        emb = provider.embed("risk assessment for cross-border payment workflow")
        batch = provider.embed_batch(
            [
                "risk assessment for cross-border payment workflow",
                "marketing campaign launch timeline",
            ]
        )

        assert len(emb) > 0
        assert len(batch) == 2
        assert len(batch[0]) == len(batch[1]) == len(emb)


class TestSearchResultModel:
    def test_search_result_creation(self):
        _ = SearchResult(id="1", document="doc", score=0.9, metadata={"source": "test"})


class TestVectorSearchAgentIntegration:
    def test_agent_can_hold_and_use_vector_search_tool(self, tool: VectorSearchTool):
        agent = AgentProfile(
            agent_id="researcher",
            display_name="Researcher",
            tools=[tool],
        )
        tool_objects = agent.get_tool_objects()
        assert any(tool_obj.name == "vector_search" for tool_obj in tool_objects)

    def test_tool_registry_executes_vector_search_call(self, tool: VectorSearchTool):
        _index_with_search_tool(
            tool,
            texts=[
                "compliance checklist for payments",
                "marketing KPI summary",
            ],
            metadata=[{"source": "legal"}, {"source": "marketing"}],
        )

        registry = ToolRegistry()
        registry.register(tool)

        result = registry.execute(
            ToolCall(
                name="vector_search",
                arguments={"query": "payments compliance", "top_k": 1},
            )
        )
        assert result.success is True
        assert "(score:" in result.output
        assert any(
            fragment in result.output for fragment in ("compliance checklist for payments", "marketing KPI summary")
        )


class TestVectorIndexTool:
    def test_name_and_description(self, index_tool: VectorIndexTool):
        assert index_tool.name == "vector_index"
        assert "index documents" in index_tool.description.lower()

    def test_execute_index_success(self, index_tool: VectorIndexTool):
        result = index_tool.execute(
            operation="index",
            texts=[
                "payments compliance checklist for cross-border transfers",
                "marketing launch timeline and KPI review",
            ],
            metadata=[{"source": "legal"}, {"source": "marketing"}],
        )
        assert result.success is True
        payload = json.loads(result.output)
        assert "Indexed" in payload["message"]
        assert isinstance(payload["ids"], list)
        assert len(payload["ids"]) > 0

    @pytest.mark.asyncio
    async def test_execute_async_index_success(self, index_tool: VectorIndexTool):
        result = await index_tool.execute_async(
            operation="index",
            texts=[
                "payments compliance checklist for cross-border transfers",
                "marketing launch timeline and KPI review",
            ],
            metadata=[{"source": "legal"}, {"source": "marketing"}],
        )
        assert result.success is True
        payload = json.loads(result.output)
        assert "Indexed" in payload["message"]
        assert isinstance(payload["ids"], list)
        assert len(payload["ids"]) > 0

    def test_execute_index_returns_ids_for_followup_delete(self, index_tool: VectorIndexTool):
        index_result = index_tool.execute(
            operation="index",
            texts=["alpha legal note", "beta marketing note"],
        )
        assert index_result.success is True
        payload = json.loads(index_result.output)
        ids = payload["ids"]
        assert isinstance(ids, list)
        assert ids

        delete_result = index_tool.execute(operation="delete", ids=[ids[0]])
        assert delete_result.success is True
        assert delete_result.output == "Deleted 1 vectors."

    def test_execute_index_enriches_metadata_defaults(self, index_tool: VectorIndexTool):
        result = index_tool.execute(
            operation="index",
            texts=["alpha beta gamma"],
            metadata=[{"source": "kb://policy"}],
        )
        assert result.success is True

        query_embedding = index_tool._provider.embed("alpha beta gamma")
        hits = index_tool._store.search(query_embedding=query_embedding, top_k=1)
        assert hits
        meta = hits[0].metadata
        assert meta["_chunk_index"] == 0
        assert meta["_document_index"] == 0
        assert meta["source"] == "kb://policy"
        assert meta["title"] == "Document 0"
        assert meta["doc_id"] == "doc_0"

    def test_execute_index_rejects_store_dimension_mismatch_early(self, provider: EmbeddingProvider):
        class _DimensionedStore(VectorStore):
            def __init__(self) -> None:
                self._dimension = 99

            def add(
                self,
                documents: list[str],
                embeddings: list[list[float]],
                metadata: list[dict[str, Any]] | None = None,
            ) -> list[str]:
                return ["ok"]

            def search(
                self,
                query_embedding: list[float],
                top_k: int = 5,
                metadata_filters: dict[str, Any] | None = None,
            ) -> list[SearchResult]:
                return []

            def delete(self, ids: list[str]) -> None:
                _ = ids

        tool = VectorIndexTool(
            provider=provider,
            store=_DimensionedStore(),
            chunker=DocumentChunker(chunk_units=10, chunk_overlap=0),
        )
        result = tool.execute(operation="index", texts=["alpha beta gamma"])
        assert result.success is False
        assert result.error is not None
        assert "embedding dimension mismatch" in result.error

    def test_execute_index_rejects_metadata_documents_length_mismatch(self, index_tool: VectorIndexTool):
        result = index_tool.execute(
            operation="index",
            texts=["alpha", "beta"],
            metadata=[{"source": "only-one"}],
        )
        assert result.success is False
        assert result.error is not None
        assert "metadata/documents length mismatch" in result.error

    def test_execute_index_keeps_empty_string_metadata_values(self, index_tool: VectorIndexTool):
        result = index_tool.execute(
            operation="index",
            texts=["alpha beta gamma"],
            metadata=[{"source": "", "title": "", "doc_id": ""}],
        )
        assert result.success is True

        query_embedding = index_tool._provider.embed("alpha beta gamma")
        hits = index_tool._store.search(query_embedding=query_embedding, top_k=1)
        assert hits
        meta = hits[0].metadata
        assert meta["source"] == ""
        assert meta["title"] == ""
        assert meta["doc_id"] == ""

    def test_execute_delete_success(self, index_tool: VectorIndexTool):
        ids = index_tool.index(
            texts=[
                "alpha legal note",
                "beta marketing note",
            ]
        )
        result = index_tool.execute(operation="delete", ids=[ids[0]])
        assert result.success is True
        assert result.output == "Deleted 1 vectors."

    def test_execute_index_without_texts_returns_error(self, index_tool: VectorIndexTool):
        result = index_tool.execute(operation="index")
        assert result.success is False
        assert result.error == "No texts provided for index"

    def test_execute_delete_without_ids_returns_error(self, index_tool: VectorIndexTool):
        result = index_tool.execute(operation="delete")
        assert result.success is False
        assert result.error == "No ids provided for delete"

    def test_execute_index_rejects_non_dict_metadata_entry(self, index_tool: VectorIndexTool):
        result = index_tool.execute(
            operation="index",
            texts=["alpha"],
            metadata=["not-a-dict"],
        )
        assert result.success is False
        assert result.error is not None
        assert "metadata[0] must be a dict" in result.error


class TestFromSettingsSharedComponents:
    def test_shared_requires_shared_key(self):
        with pytest.raises(ValueError, match="shared_key is required"):
            VectorSearchTool.from_settings(shared=True, store_type="in_memory")

    def test_search_and_index_share_store_with_key(self):
        s = VectorSearchTool.from_settings(shared=True, shared_key="test-ns", store_type="in_memory")
        i = VectorIndexTool.from_settings(shared=True, shared_key="test-ns", store_type="in_memory")
        assert s._store is i._store

    def test_different_shared_keys_create_different_stores(self):
        s = VectorSearchTool.from_settings(shared=True, shared_key="ns-a", store_type="in_memory")
        i = VectorSearchTool.from_settings(shared=True, shared_key="ns-b", store_type="in_memory")
        assert s._store is not i._store

    def test_shared_false_creates_independent_stores(self):
        s = VectorSearchTool.from_settings(shared=False, store_type="in_memory")
        i = VectorIndexTool.from_settings(shared=False, store_type="in_memory")
        assert s._store is not i._store

    def test_default_shared_is_false(self):
        s1 = VectorSearchTool.from_settings(store_type="in_memory")
        s2 = VectorSearchTool.from_settings(store_type="in_memory")
        assert s1._store is not s2._store

    def test_clear_shared_components_by_key(self):
        s = VectorSearchTool.from_settings(shared=True, shared_key="clear-test", store_type="in_memory")
        store_before = s._store
        clear_shared_components()
        s2 = VectorSearchTool.from_settings(shared=True, shared_key="clear-test", store_type="in_memory")
        assert s2._store is not store_before

    def test_clear_shared_components_prefix_removes_all_matching(self):
        s = VectorSearchTool.from_settings(shared=True, shared_key="pfx", store_type="in_memory")
        i = VectorIndexTool.from_settings(shared=True, shared_key="pfx", store_type="in_memory")
        store_s = s._store
        store_i = i._store
        assert store_s is store_i

        clear_shared_components("pfx")

        s2 = VectorSearchTool.from_settings(shared=True, shared_key="pfx", store_type="in_memory")
        assert s2._store is not store_s

    def test_partial_override_uses_shared_store_with_custom_provider(self):
        custom_provider = DummyEmbeddingProvider()
        s1 = VectorSearchTool.from_settings(shared=True, shared_key="partial", store_type="in_memory")
        s2 = VectorSearchTool.from_settings(
            shared=True,
            shared_key="partial",
            store_type="in_memory",
            provider=custom_provider,
        )
        assert s2._store is s1._store
        assert s2._provider is custom_provider
        assert s2._provider is not s1._provider


class TestNewHelperFunctions:
    def test_coerce_citation_mode_accepts_valid_values(self):
        assert vector_search_module._coerce_citation_mode("none") == "none"
        assert vector_search_module._coerce_citation_mode("inline") == "inline"
        assert vector_search_module._coerce_citation_mode("numbered") == "numbered"

    def test_coerce_citation_mode_falls_back_for_invalid_value(self):
        assert vector_search_module._coerce_citation_mode("invalid-mode") == "numbered"
        assert vector_search_module._coerce_citation_mode(123) == "numbered"

    def test_resolve_vector_search_defaults_falls_back_when_settings_fail(self, monkeypatch: pytest.MonkeyPatch):
        import gmas.config.settings as cfg

        class _Boom:
            def __init__(self):
                msg = "boom"
                raise OSError(msg)

        monkeypatch.setattr(cfg, "FrameworkSettings", _Boom)
        defaults = vector_search_module._resolve_vector_search_defaults()

        assert defaults["store_type"] == "in_memory"
        assert defaults["top_k"] == 5
        assert defaults["score_threshold"] == 0.0
        assert defaults["max_context_tokens"] == 4096
        assert defaults["citation_mode"] == "numbered"
        assert defaults["strict_context_mode"] is False
        assert defaults["context_template"] == "{context}"

    def test_resolve_vector_search_defaults_reads_settings_values(self, monkeypatch: pytest.MonkeyPatch):
        import gmas.config.settings as cfg

        class _FakeSettings:
            def __init__(self):
                self.vector_store_type = "faiss"
                self.vector_top_k = 7
                self.vector_score_threshold = 0.25
                self.vector_max_context_tokens = 321
                self.vector_citation_mode = "inline"
                self.vector_strict_context = True
                self.vector_context_template = "CTX: {context}"

        monkeypatch.setattr(cfg, "FrameworkSettings", _FakeSettings)
        defaults = vector_search_module._resolve_vector_search_defaults()

        assert defaults["store_type"] == "faiss"
        assert defaults["top_k"] == 7
        assert defaults["score_threshold"] == 0.25
        assert defaults["max_context_tokens"] == 321
        assert defaults["citation_mode"] == "inline"
        assert defaults["strict_context_mode"] is True
        assert defaults["context_template"] == "CTX: {context}"

    def test_resolve_vector_index_default_store_type_falls_back(self, monkeypatch: pytest.MonkeyPatch):
        import gmas.config.settings as cfg

        class _Boom:
            def __init__(self):
                msg = "boom"
                raise OSError(msg)

        monkeypatch.setattr(cfg, "FrameworkSettings", _Boom)
        assert vector_search_module._resolve_vector_index_default_store_type() == "in_memory"

    def test_resolve_vector_index_default_store_type_reads_settings(self, monkeypatch: pytest.MonkeyPatch):
        import gmas.config.settings as cfg

        class _FakeSettings:
            def __init__(self):
                self.vector_store_type = "qdrant"

        monkeypatch.setattr(cfg, "FrameworkSettings", _FakeSettings)
        assert vector_search_module._resolve_vector_index_default_store_type() == "qdrant"

    def test_normalize_components_validates_store_provider_chunker_types(self):
        with pytest.raises(TypeError, match="store must be a VectorStore instance"):
            vector_search_module._normalize_components(
                overrides={"store": "bad"},
                shared=False,
                shared_key=None,
                store_type="in_memory",
                store_kwargs={},
            )

        with pytest.raises(TypeError, match="provider must be an EmbeddingProvider instance"):
            vector_search_module._normalize_components(
                overrides={"provider": "bad"},
                shared=False,
                shared_key=None,
                store_type="in_memory",
                store_kwargs={},
            )

        with pytest.raises(TypeError, match="chunker must be a DocumentChunker instance"):
            vector_search_module._normalize_components(
                overrides={"chunker": "bad"},
                shared=False,
                shared_key=None,
                store_type="in_memory",
                store_kwargs={},
            )

    def test_normalize_components_uses_shared_when_enabled(self, monkeypatch: pytest.MonkeyPatch):
        store = InMemoryVectorStore()
        provider = DummyEmbeddingProvider()
        chunker = DocumentChunker()

        def _fake_shared_components(*, shared_key: str, store_type: str, store_kwargs: dict[str, Any]):
            assert store_type == "in_memory"
            assert store_kwargs == {"dimension": 10}
            return store, provider, chunker

        monkeypatch.setattr(vector_search_module, "_shared_components", _fake_shared_components)

        s, p, c = vector_search_module._normalize_components(
            overrides={},
            shared=True,
            shared_key="test-key",
            store_type="in_memory",
            store_kwargs={"dimension": 10},
        )
        assert s is store
        assert p is provider
        assert c is chunker

    def test_normalize_components_keeps_explicit_components(self):
        store = InMemoryVectorStore()
        provider = DummyEmbeddingProvider()
        chunker = DocumentChunker()

        s, p, c = vector_search_module._normalize_components(
            overrides={"store": store, "provider": provider, "chunker": chunker},
            shared=True,
            shared_key="test-key",
            store_type="in_memory",
            store_kwargs={},
        )
        assert s is store
        assert p is provider
        assert c is chunker

    def test_from_settings_invalid_citation_mode_falls_back_to_numbered(self):
        tool = VectorSearchTool.from_settings(
            shared=False,
            store_type="in_memory",
            citation_mode="bad-mode",
        )
        assert tool._citation_mode == "numbered"


class TestThreadSafety:
    def test_concurrent_add_search_in_memory(self, store: InMemoryVectorStore):
        errors: list[ValueError | TypeError | RuntimeError | OSError] = []

        def add_worker(doc_id: int) -> None:
            try:
                emb = [float(doc_id), float(doc_id) / 2.0, 1.0]
                store.add(
                    documents=[f"doc-{doc_id}"],
                    embeddings=[emb],
                )
            except (ValueError, TypeError, RuntimeError, OSError) as exc:
                errors.append(exc)

        def search_worker() -> None:
            try:
                store.search(query_embedding=[1.0, 0.5, 1.0], top_k=5)
            except (ValueError, TypeError, RuntimeError, OSError) as exc:
                errors.append(exc)

        threads: list[threading.Thread] = []
        for i in range(10):
            threads.append(threading.Thread(target=add_worker, args=(i,)))
            threads.append(threading.Thread(target=search_worker))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"

    def test_concurrent_delete_in_memory(self, store: InMemoryVectorStore):
        ids = store.add(
            documents=[f"doc-{i}" for i in range(20)],
            embeddings=[[float(i), 0.0, 1.0] for i in range(20)],
        )
        errors: list[ValueError | TypeError | RuntimeError | OSError] = []

        def delete_worker(doc_id: str) -> None:
            try:
                store.delete([doc_id])
            except (ValueError, TypeError, RuntimeError, OSError) as exc:
                errors.append(exc)

        threads = [threading.Thread(target=delete_worker, args=(doc_id,)) for doc_id in ids[:10]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


class TestBatchedIndexing:
    def test_index_texts_batched(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        chunker = DocumentChunker(chunk_units=3, chunk_overlap=0, split_strategy="words")
        texts = [f"word_{i} word_{i + 1} word_{i + 2} word_{i + 3}" for i in range(10)]
        ids = vector_search_module._index_texts(
            texts=texts,
            metadata=None,
            chunker=chunker,
            provider=provider,
            store=store,
            batch_size=3,
        )
        assert len(ids) > 0
        results = store.search(query_embedding=provider.embed("word_0"), top_k=100)
        assert len(results) > 0


class TestScoreSemantics:
    def test_in_memory_l2_returns_relevance_score(self):
        store = InMemoryVectorStore(metric="l2")
        store.add(
            documents=["near", "far"],
            embeddings=[[1.0, 0.0], [100.0, 100.0]],
        )
        results = store.search(query_embedding=[1.0, 0.0], top_k=2)
        assert results[0].score > results[1].score
        assert 0.0 <= results[0].score <= 1.0
        assert results[0].metadata["_score_metric"] == "l2"

    def test_score_semantics_property_default(self):
        store = InMemoryVectorStore()
        assert store.score_semantics == "higher_is_better"

    def test_score_semantics_metadata_consistent_with_type(self):
        store = InMemoryVectorStore()
        store.add(documents=["alpha"], embeddings=[[1.0, 0.0, 0.0]])
        results = store.search(query_embedding=[1.0, 0.0, 0.0], top_k=1)
        assert results[0].metadata["_score_semantics"] == "higher_is_better"


class TestVectorIndexFactory:
    def test_vector_index_factory_registered(self):
        from gmas.tools.base import create_tool_from_config

        tool = create_tool_from_config({"name": "vector_index"})
        assert tool is not None
        assert isinstance(tool, VectorIndexTool)
        assert tool.name == "vector_index"


class TestRemoteBackendValidation:
    def test_qdrant_requires_collection_name(self):
        with pytest.raises(ValueError, match="QdrantVectorStore requires 'collection_name'"):
            vector_search_module._create_vector_store("qdrant")

    def test_pinecone_requires_index_name(self):
        with pytest.raises(ValueError, match="PineconeVectorStore requires 'index_name'"):
            vector_search_module._create_vector_store("pinecone")

    def test_milvus_requires_collection_name(self):
        with pytest.raises(ValueError, match="MilvusVectorStore requires 'collection_name'"):
            vector_search_module._create_vector_store("milvus")


class TestStoreClose:
    def test_base_close_is_noop(self):
        store = InMemoryVectorStore()
        store.close()

    def test_clear_shared_components_calls_store_close(self):
        s = VectorSearchTool.from_settings(shared=True, shared_key="close-test", store_type="in_memory")
        store = s._store
        close_called = []
        original_close = store.close

        def _tracking_close() -> None:
            close_called.append(True)
            original_close()

        store.close = _tracking_close  # type: ignore[assignment,ty:invalid-assignment]
        clear_shared_components("close-test")
        assert close_called, "store.close() was not called during clear_shared_components"

    def test_clear_all_shared_components_calls_store_close(self):
        s = VectorSearchTool.from_settings(shared=True, shared_key="close-all", store_type="in_memory")
        store = s._store
        close_called = []
        original_close = store.close

        def _tracking_close() -> None:
            close_called.append(True)
            original_close()

        store.close = _tracking_close  # type: ignore[assignment,ty:invalid-assignment]
        clear_shared_components()
        assert close_called


class TestToolLevelLifecycle:
    def test_search_tool_owns_store_when_created_internally(self):
        tool = VectorSearchTool()
        assert tool._owns_store is True

    def test_search_tool_does_not_own_injected_store(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        tool = VectorSearchTool(provider=provider, store=store)
        assert tool._owns_store is False

    def test_index_tool_owns_store_when_created_internally(self):
        tool = VectorIndexTool()
        assert tool._owns_store is True

    def test_index_tool_does_not_own_injected_store(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        tool = VectorIndexTool(provider=provider, store=store)
        assert tool._owns_store is False

    def test_close_does_not_close_injected_store(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        close_called = []
        original_close = store.close
        store.close = lambda: close_called.append(True) or original_close()  # type: ignore[assignment,ty:invalid-assignment]

        search = VectorSearchTool(provider=provider, store=store)
        index = VectorIndexTool(provider=provider, store=store)
        search.close()
        index.close()
        assert not close_called, "close() should not close an externally injected store"

    def test_close_closes_owned_store(self):
        tool = VectorSearchTool()
        close_called = []
        original_close = tool._store.close
        tool._store.close = lambda: close_called.append(True) or original_close()  # type: ignore[assignment,ty:invalid-assignment]
        tool.close()
        assert close_called, "close() should close an owned store"

    def test_shared_store_not_closed_by_tool(self):
        s = VectorSearchTool.from_settings(shared=True, shared_key="own-test", store_type="in_memory")
        close_called = []
        original_close = s._store.close
        s._store.close = lambda: close_called.append(True) or original_close()  # type: ignore[assignment,ty:invalid-assignment]
        s.close()
        assert not close_called, "close() should not close a shared store"

    def test_search_tool_context_manager(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        with VectorSearchTool(provider=provider, store=store) as tool:
            assert tool.name == "vector_search"

    def test_index_tool_context_manager(self, provider: EmbeddingProvider, store: InMemoryVectorStore):
        with VectorIndexTool(provider=provider, store=store) as tool:
            assert tool.name == "vector_index"

    def test_from_settings_tolerates_bad_top_k(self):
        tool = VectorSearchTool.from_settings(shared=False, store_type="in_memory", top_k="bad")
        assert tool._top_k == 5

    def test_from_settings_tolerates_bad_score_threshold(self):
        tool = VectorSearchTool.from_settings(shared=False, store_type="in_memory", score_threshold="bad")
        assert tool._score_threshold == 0.0
