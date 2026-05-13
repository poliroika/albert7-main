"""
Vector search -- end-to-end example.

Demonstrates the full lifecycle of the vector search tool pack:
  1. Direct tool usage: index documents, search, get JSON results
  2. Shared store between search and index tools
  3. Context-manager / ownership semantics
  4. Agent-driven RAG pipeline (requires LLM)

No external vector DB needed -- uses the built-in in-memory store
with a deterministic hash-based embedding provider.

Run:
    python -m examples.vector_search_example
"""

import json
import os

from gmas.tools.vector_search import (
    SentenceTransformerProvider,
    VectorIndexTool,
    VectorSearchTool,
)
from gmas.utils import configure_console

CORPUS = [
    "Python is a high-level, interpreted programming language known for its "
    "readability and versatility.  It supports multiple paradigms including "
    "procedural, object-oriented, and functional programming.",
    "Rust is a systems programming language focused on safety, speed, and "
    "concurrency.  Its ownership model guarantees memory safety without a "
    "garbage collector.",
    "JavaScript is the language of the web.  It runs in browsers and on "
    "servers via Node.js, enabling full-stack development with a single "
    "language.",
    "Go (Golang) is a statically typed, compiled language designed at Google. "
    "It excels at concurrent programming with goroutines and channels.",
]

METADATA = [
    {"lang": "python", "paradigm": "multi"},
    {"lang": "rust", "paradigm": "systems"},
    {"lang": "javascript", "paradigm": "multi"},
    {"lang": "go", "paradigm": "concurrent"},
]


def _header(title: str) -> None:
    print(f"\n{'-' * 60}\n  {title}\n{'-' * 60}")


# -- Example 1: Direct tool usage ------------------------------------------


def example_direct_usage():
    """Index four documents, then search -- all without an LLM."""
    _header("1 - Direct Tool Usage (no LLM)")

    provider = SentenceTransformerProvider(model_name="hash:64", normalize=True, batch_size=8)

    with VectorSearchTool(
        provider=provider,
        citation_mode="numbered",
        max_context_tokens=512,
        return_mode="context",
    ) as search:
        index = VectorIndexTool(
            store=search.store,
            provider=search.provider,
            chunker=search.chunker,
        )

        print("\n  Indexing 4 documents ...")
        result = index.execute(operation="index", texts=CORPUS, metadata=METADATA)
        payload = json.loads(result.output)
        print(f"  -> {payload['indexed_chunks']} chunks from {payload['documents']} docs")

        print("\n  a) Search: 'memory safety without garbage collection'")
        result = search.execute(query="memory safety without garbage collection", top_k=2)
        print(f"  {result.output}\n")

        print("  b) Search with metadata filter: lang=python")
        result = search.execute(
            query="programming paradigms",
            top_k=2,
            filters={"lang": "python"},
        )
        print(f"  {result.output}\n")

        print("  c) JSON return mode:")
        result = search.execute(
            query="concurrent programming",
            top_k=2,
            return_mode="json",
        )
        data = json.loads(result.output)
        for r in data["results"]:
            print(f"    score={r['score']:.4f}  lang={r['metadata'].get('lang', '?')}")


# -- Example 2: Shared store -----------------------------------------------


def example_shared_store():
    """Two tools share the same store via from_settings(shared=True)."""
    _header("2 - Shared Store via from_settings")

    search = VectorSearchTool.from_settings(
        shared=True,
        shared_key="demo-ns",
        store_type="in_memory",
    )
    index = VectorIndexTool.from_settings(
        shared=True,
        shared_key="demo-ns",
        store_type="in_memory",
    )
    print("  Shared namespace: demo-ns")

    index.execute(operation="index", texts=CORPUS[:2], metadata=METADATA[:2])
    result = search.execute(query="interpreted language", top_k=1)
    print(f"  Search result:\n  {result.output}")


# -- Example 3: Ownership & lifecycle --------------------------------------


def example_ownership():
    """Show that close() respects store ownership."""
    _header("3 - Ownership Semantics")

    from gmas.tools.vector_search import InMemoryVectorStore

    external_store = InMemoryVectorStore()

    tool_a = VectorSearchTool(store=external_store)
    tool_b = VectorSearchTool()

    print(f"  tool_a owns store? {tool_a.owns_store}  (injected -> False)")
    print(f"  tool_b owns store? {tool_b.owns_store}  (self-created -> True)")

    tool_a.close()
    tool_b.close()
    print("  Both tools closed -- external store still usable")

    external_store.add(documents=["still alive"], embeddings=[[1.0, 0.0, 0.0]])
    print(f"  external_store doc count: {len(external_store.ids)}")


# -- Example 4: Agent-driven RAG -------------------------------------------


def example_agent_rag():
    """Full RAG pipeline: index -> agent searches -> answers from context."""
    _header("4 - Agent-Driven RAG Pipeline (requires LLM)")

    api_key = os.getenv("LLM_API_KEY", "")
    if not api_key:
        print("  Skipped -- set LLM_API_KEY to run this example.")
        return

    from gmas.builder import GraphBuilder
    from gmas.execution import MACPRunner
    from gmas.tools import create_openai_caller, get_registry

    provider = SentenceTransformerProvider(model_name="hash:64", normalize=True)
    search = VectorSearchTool(
        provider=provider,
        context_guardrail=True,
        citation_mode="numbered",
    )
    index = VectorIndexTool(
        store=search.store,
        provider=search.provider,
        chunker=search.chunker,
    )

    index.execute(operation="index", texts=CORPUS, metadata=METADATA)
    get_registry().register(search)

    llm = create_openai_caller(
        base_url=os.getenv("LLM_BASE_URL", "http://localhost:8000/v1"),
        api_key=api_key,
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        temperature=0.1,
    )

    builder = GraphBuilder()
    builder.add_agent(
        agent_id="rag_agent",
        display_name="RAG Agent",
        persona="a helpful programming assistant",
        description="Answers questions using vector search over a knowledge base.",
        tools=["vector_search"],
    )
    builder.add_task(query="Which language guarantees memory safety without a garbage collector?")
    builder.connect_task_to_agents(agent_ids=["rag_agent"])

    result = MACPRunner(llm_caller=llm).run_round(builder.build())
    print(f"  Answer: {result.final_answer}")


# -- Main -----------------------------------------------------------------


def main():
    configure_console()

    example_direct_usage()
    example_shared_store()
    example_ownership()
    example_agent_rag()

    print(f"\n{'=' * 60}")
    print("All vector search examples completed.")


if __name__ == "__main__":
    main()
