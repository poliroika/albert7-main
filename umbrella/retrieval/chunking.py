"""
Document chunking for retrieval indexing.

This module handles splitting documents into chunks for lexical indexing.
"""

import logging
import re
from typing import List

from umbrella.retrieval.models import (
    Chunk,
    SourceDocument,
)

log = logging.getLogger(__name__)


def chunk_document(
    source: SourceDocument,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> list[Chunk]:
    """
    Split a source document into chunks for indexing.

    Args:
        source: The source document to chunk
        chunk_size: Target size for chunks (in characters)
        chunk_overlap: Overlap between chunks

    Returns:
        List of Chunk objects.
    """
    if not source.content:
        return []

    # Choose chunking strategy based on source type
    if source.path.suffix == ".md":
        return _chunk_markdown(source, chunk_size, chunk_overlap)
    elif source.path.suffix == ".py":
        return _chunk_python_code(source, chunk_size, chunk_overlap)
    else:
        return _chunk_generic(source, chunk_size, chunk_overlap)


def _chunk_markdown(
    source: SourceDocument,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """Chunk a markdown document by headings and paragraphs."""
    chunks = []
    content = source.content

    # Split by headings
    heading_pattern = re.compile(r"^#{1,6}\s+.*$", re.MULTILINE)
    sections = []
    current_section = {"content": "", "heading": "Root", "level": 0}

    for line in content.split("\n"):
        match = heading_pattern.match(line)
        if match:
            # Save previous section
            if current_section["content"].strip():
                sections.append(current_section.copy())

            # Start new section
            level = len(re.match(r"^(#+)", line).group(0))
            heading = line.lstrip("#").strip()
            current_section = {
                "content": "",
                "heading": heading,
                "level": level,
            }
        else:
            current_section["content"] += line + "\n"

    # Add last section
    if current_section["content"].strip():
        sections.append(current_section)

    # Chunk sections by size
    chunk_num = 0
    for section in sections:
        section_content = section["content"]
        if not section_content.strip():
            continue

        # If section is small, keep as one chunk
        if len(section_content) <= chunk_size:
            chunk = _create_chunk(
                source,
                section_content,
                chunk_num,
                "markdown_section",
                {"heading": section["heading"]},
            )
            chunks.append(chunk)
            chunk_num += 1
        else:
            # Split large section into paragraphs
            paragraphs = section_content.split("\n\n")
            current_chunk = ""
            line_num = 0

            for para in paragraphs:
                if len(current_chunk) + len(para) <= chunk_size:
                    current_chunk += para + "\n\n"
                else:
                    if current_chunk.strip():
                        chunk = _create_chunk(
                            source,
                            current_chunk.strip(),
                            chunk_num,
                            "markdown_paragraphs",
                            {"heading": section["heading"]},
                        )
                        chunks.append(chunk)
                        chunk_num += 1

                    # Start new chunk
                    current_chunk = para + "\n\n"

            # Add remaining content
            if current_chunk.strip():
                chunk = _create_chunk(
                    source,
                    current_chunk.strip(),
                    chunk_num,
                    "markdown_paragraphs",
                    {"heading": section["heading"]},
                )
                chunks.append(chunk)
                chunk_num += 1

    return chunks


def _chunk_python_code(
    source: SourceDocument,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """Chunk Python code by classes and functions."""
    chunks = []
    content = source.content

    # Split by top-level definitions (classes, functions)
    # This is a simple heuristic - proper parsing should use AST
    lines = content.split("\n")
    current_chunk = []
    chunk_start = 0
    line_num = 0
    chunk_count = 0
    indent_level = 0

    for line in lines:
        stripped = line.lstrip()
        current_indent = len(line) - len(stripped)

        # New top-level definition
        if stripped and not stripped.startswith("#") and current_indent == 0:
            if (
                stripped.startswith("class ")
                or stripped.startswith("def ")
                or stripped.startswith("async def ")
            ):
                # Save previous chunk if it's large enough
                if len("\n".join(current_chunk)) > 100:
                    chunk_content = "\n".join(current_chunk)
                    chunk = _create_chunk(
                        source,
                        chunk_content,
                        chunk_count,
                        "python_definition",
                        {"start_line": chunk_start, "end_line": line_num},
                        chunk_start,
                        line_num,
                    )
                    chunks.append(chunk)
                    chunk_count += 1

                # Start new chunk
                current_chunk = [line]
                chunk_start = line_num
            else:
                current_chunk.append(line)
        else:
            current_chunk.append(line)

        line_num += 1

    # Add final chunk
    if current_chunk:
        chunk_content = "\n".join(current_chunk)
        chunk = _create_chunk(
            source,
            chunk_content,
            chunk_count,
            "python_definition",
            {"start_line": chunk_start, "end_line": line_num},
            chunk_start,
            line_num,
        )
        chunks.append(chunk)

    return chunks


def _chunk_generic(
    source: SourceDocument,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """Chunk a generic document by size with overlap."""
    chunks = []
    content = source.content

    start = 0
    chunk_num = 0
    line_starts = [0]
    current_pos = 0

    # Find line start positions
    for match in re.finditer(r"\n", content):
        line_starts.append(match.start() + 1)

    line_starts.append(len(content))

    while start < len(content):
        # Find end position
        end = min(start + chunk_size, len(content))

        # Try to break at a line boundary
        for line_start in line_starts:
            if line_start > end:
                end = line_starts[line_starts.index(line_start) - 1]
                break

        chunk_content = content[start:end].strip()
        if chunk_content:
            chunk = _create_chunk(
                source,
                chunk_content,
                chunk_num,
                "generic",
                {},
            )
            chunks.append(chunk)
            chunk_num += 1

        # Move start with overlap
        start = max(start + 1, end - chunk_overlap)

    return chunks


def _create_chunk(
    source: SourceDocument,
    content: str,
    chunk_num: int,
    chunk_type: str,
    metadata: dict,
    start_line: int = 0,
    end_line: int = 0,
) -> Chunk:
    """Create a Chunk object."""
    # Generate unique chunk ID
    chunk_id = _generate_chunk_id(source.source_id, chunk_num)

    # Update metadata
    final_metadata = {
        "source_title": source.title,
        "source_type": source.source_type.value,
        "source_language": source.language,
        "source_category": source.category,
        **metadata,
    }

    return Chunk(
        chunk_id=chunk_id,
        source_id=source.source_id,
        content=content,
        chunk_type=chunk_type,
        start_line=start_line,
        end_line=end_line,
        metadata=final_metadata,
    )


def _generate_chunk_id(source_id: str, chunk_num: int) -> str:
    """Generate a unique chunk ID."""
    return f"{source_id}_chunk_{chunk_num}"


def merge_chunks_for_retrieval(
    chunks: list[Chunk],
    max_chunks: int = 3,
) -> str:
    """
    Merge chunks for retrieval display.

    Args:
        chunks: List of chunks to merge
        max_chunks: Maximum number of chunks to include

    Returns:
        Merged content with separators.
    """
    selected = chunks[:max_chunks]
    parts = []

    for i, chunk in enumerate(selected):
        prefix = f"[{i + 1}/{len(selected)}] "
        parts.append(prefix + chunk.content)

    return "\n\n---\n\n".join(parts)
