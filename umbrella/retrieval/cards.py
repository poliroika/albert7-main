"""
Retrieval card generation for manager consumption.

This module converts search results into structured actionable cards.
"""

import logging
from typing import List

from umbrella.retrieval.models import (
    RetrievalCard,
    RetrievalQuery,
    RetrievalHit,
    SourceType,
    SymbolType,
)

log = logging.getLogger(__name__)


def build_retrieval_card(
    query: RetrievalQuery,
    hits: list[RetrievalHit],
    confidence_threshold: float = 0.1,
) -> RetrievalCard:
    """
    Build a retrieval card from search results.

    Args:
        query: The original query
        hits: List of retrieval hits
        confidence_threshold: Minimum score to include

    Returns:
        A structured RetrievalCard for manager consumption.
    """
    # Filter hits by score
    relevant_hits = [h for h in hits if h.score >= confidence_threshold]

    if not relevant_hits:
        return _create_fallback_card(query)

    # Extract key symbols
    key_symbols = _extract_key_symbols(relevant_hits)

    # Extract key files
    key_files = _extract_key_files(relevant_hits)

    # Extract example usage
    example_usage = _extract_example_usage(relevant_hits)

    # Extract documentation references
    doc_references = _extract_doc_references(relevant_hits)

    # Identify anti-patterns
    anti_patterns = _identify_anti_patterns(relevant_hits)

    # Suggest edit locations
    suggested_locations = _suggest_edit_locations(relevant_hits, query)

    # Determine recommended pattern
    recommended_pattern = _determine_pattern(relevant_hits, query)

    # Calculate confidence
    confidence = _calculate_confidence(relevant_hits)

    return RetrievalCard(
        query=query.query,
        recommended_pattern=recommended_pattern,
        key_symbols=key_symbols,
        key_files=key_files,
        example_usage=example_usage,
        doc_references=doc_references,
        anti_patterns=anti_patterns,
        suggested_edit_locations=suggested_locations,
        hits=relevant_hits[:5],  # Top 5 for reference
        confidence=confidence,
    )


def _create_fallback_card(query: RetrievalQuery) -> RetrievalCard:
    """Create a fallback card when no relevant hits found."""
    return RetrievalCard(
        query=query.query,
        recommended_pattern="No specific pattern found - consult GMAS documentation",
        key_symbols=[],
        key_files=[],
        example_usage=["No examples found - check gmas/docs/"],
        doc_references=["gmas/README.md", "gmas/QUICKSTART.md"],
        anti_patterns=["Don't invent GMAS APIs - check documentation first"],
        suggested_edit_locations=["gmas/src/gmas/**", "gmas/docs/**"],
        hits=[],
        confidence=0.0,
    )


def _extract_key_symbols(hits: list[RetrievalHit]) -> list[str]:
    """Extract key GMAS symbols from hits."""
    symbols = set()

    for hit in hits:
        if hit.symbol_name:
            symbols.add(hit.symbol_name)

        # Extract symbols from metadata
        metadata = hit.metadata
        if metadata.get("parent_class"):
            symbols.add(metadata["parent_class"])
        if metadata.get("parent_module"):
            symbols.add(metadata["parent_module"])

    # Filter out None values and sort
    return sorted([s for s in symbols if s])[:10]


def _extract_key_files(hits: list[RetrievalHit]) -> list[str]:
    """Extract key file paths from hits."""
    files: set[str] = set()

    for hit in hits:
        if hit.path:
            # Convert to repo-relative path
            path_str = str(hit.path)
            if "gmas/" in path_str:
                # Extract gmas/... path
                idx = path_str.index("gmas/")
                files.add(path_str[idx:])
            else:
                files.add(path_str)

    return sorted(list(files))[:10]


def _extract_example_usage(hits: list[RetrievalHit]) -> list[str]:
    """Extract example code snippets from hits."""
    examples = []

    for hit in hits:
        content = hit.content

        # Look for code blocks in markdown
        if "```" in content:
            blocks = _extract_code_blocks(content)
            examples.extend(blocks[:2])  # Max 2 per hit

        # Look for function/class definitions
        if hit.symbol_type in [SymbolType.FUNCTION, SymbolType.METHOD]:
            if hit.excerpt:
                examples.append(f"{hit.symbol_name}: {hit.excerpt}")

        if len(examples) >= 5:
            break

    return examples[:5]


def _extract_code_blocks(content: str) -> list[str]:
    """Extract code blocks from markdown content."""
    blocks = []
    in_code = False
    current_block = []

    for line in content.split("\n"):
        if line.strip().startswith("```"):
            if in_code:
                # End of code block
                if current_block:
                    blocks.append("\n".join(current_block))
                current_block = []
                in_code = False
            else:
                # Start of code block
                in_code = True
        elif in_code:
            current_block.append(line)

    return blocks


def _extract_doc_references(hits: list[RetrievalHit]) -> list[str]:
    """Extract documentation page references."""
    docs = set()

    for hit in hits:
        if hit.source_type == SourceType.DOCUMENTATION:
            if hit.path:
                path_str = str(hit.path)
                # Extract relative path from docs/
                if "/docs/" in path_str:
                    idx = path_str.index("/docs/")
                    docs.add(path_str[idx + 1 :])
                elif path_str.endswith(".md"):
                    docs.add(hit.path.name)

    return sorted(list(docs))[:10]


def _identify_anti_patterns(hits: list[RetrievalHit]) -> list[str]:
    """Identify common anti-patterns to avoid."""
    anti_patterns = []

    # Based on query and hits, suggest what NOT to do
    for hit in hits:
        content = hit.content.lower()

        # Look for warnings in documentation
        if "deprecated" in content or "don't" in content:
            if "```" in content:
                # Extract context around warning
                lines = content.split("\n")
                for i, line in enumerate(lines):
                    if "deprecated" in line.lower() or "don't" in line.lower():
                        context = "\n".join(lines[max(0, i - 2) : i + 3])
                        anti_patterns.append(context[:150])
                        break

    # Add common GMAS anti-patterns
    common_anti = [
        "Don't import gmas components directly - use gmas tools and APIs",
        "Don't reimplement graph execution - use MACPRunner",
        "Don't skip ToolRegistry - use create_tool_from_config()",
        "Don't bypass memory system - use AgentMemory and SharedMemoryPool",
    ]

    for pattern in common_anti:
        if pattern not in anti_patterns:
            anti_patterns.append(pattern)

    return anti_patterns[:5]


def _suggest_edit_locations(
    hits: list[RetrievalHit],
    query: RetrievalQuery,
) -> list[str]:
    """Suggest where to make changes in a workspace."""
    locations = []

    # Based on hit types, suggest appropriate locations
    for hit in hits:
        if hit.symbol_type == SymbolType.CLASS:
            locations.append(f"Extend {hit.symbol_name} or create similar class")
        elif hit.symbol_type == SymbolType.FUNCTION:
            locations.append(f"Use pattern from {hit.symbol_name}()")
        elif hit.source_type == SourceType.EXAMPLE:
            locations.append(f"Follow example in {hit.path.name}")
        elif hit.source_type == SourceType.DOCUMENTATION:
            if "user-guide" in str(hit.path):
                locations.append(f"Follow user guide: {hit.path.name}")

    # Add workspace-specific locations
    workspace_locs = [
        "graph/topology.toml - Define agent graph",
        "agents/ - Agent profiles and prompts",
        "prompts/ - Prompt templates",
        "tools/allowlist.toml - Available tools",
        "models/models.toml - Model configuration",
    ]

    for loc in workspace_locs:
        locations.append(loc)

    return locations[:8]


def _determine_pattern(hits: list[RetrievalHit], query: RetrievalQuery) -> str:
    """Determine the recommended GMAS pattern."""
    query_lower = query.query.lower()

    # Check for common GMAS patterns
    if "runner" in query_lower or "execute" in query_lower:
        return "Use MACPRunner to execute agent graphs"
    elif "tool" in query_lower:
        return "Use ToolRegistry with create_tool_from_config() for tool management"
    elif "memory" in query_lower:
        return "Use AgentMemory and SharedMemoryPool for agent state management"
    elif "graph" in query_lower:
        return "Build graphs using AutoGraphBuilder or define in topology.toml"
    elif "agent" in query_lower:
        return "Define agents using AgentProfile with system prompt and tools"
    elif "callback" in query_lower:
        return "Use callback handlers for event handling (see gmas/callbacks/)"

    # Look at top hits for pattern hints
    if hits:
        top_hit = hits[0]
        if top_hit.symbol_type == SymbolType.CLASS:
            return f"Use {top_hit.symbol_name} pattern from GMAS"
        elif top_hit.symbol_type == SymbolType.FUNCTION:
            return f"Use {top_hit.symbol_name}() pattern from GMAS"

    return "Consult GMAS documentation for the appropriate pattern"


def _calculate_confidence(hits: list[RetrievalHit]) -> float:
    """Calculate confidence score for the retrieval results."""
    if not hits:
        return 0.0

    # Based on hit scores and diversity
    avg_score = sum(h.score for h in hits) / len(hits)

    # Boost if we have diverse sources
    source_types = {h.source_type for h in hits}
    diversity_bonus = len(source_types) * 0.1

    # Boost if we have both docs and code
    has_docs = any(h.source_type == SourceType.DOCUMENTATION for h in hits)
    has_code = any(h.source_type == SourceType.SOURCE_CODE for h in hits)
    completeness_bonus = 0.2 if (has_docs and has_code) else 0.0

    confidence = min(1.0, avg_score * 0.1 + diversity_bonus + completeness_bonus)

    return round(confidence, 2)
