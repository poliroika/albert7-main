"""
Documentation index with mkdocs.yml hierarchy parsing.

This module parses the mkdocs.yml navigation structure to understand
the documentation hierarchy and provides enhanced doc search.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from umbrella.retrieval.models import SourceDocument, SourceType

log = logging.getLogger(__name__)


@dataclass
class MkDocsNavNode:
    """A node in the mkdocs navigation hierarchy."""

    title: str
    path: str | None  # None for section headers
    children: list["MkDocsNavNode"] = field(default_factory=list)
    parent: Optional["MkDocsNavNode"] = None
    section: str = ""  # e.g., "user-guide", "api"

    def get_full_path(self) -> str:
        """Get the full hierarchical path string."""
        parts = []
        current = self.parent
        while current:
            if current.title:
                parts.append(current.title)
            current = current.parent
        parts.reverse()
        if self.title:
            parts.append(self.title)
        return " > ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "title": self.title,
            "path": self.path,
            "section": self.section,
            "full_path": self.get_full_path(),
            "has_children": len(self.children) > 0,
        }


@dataclass
class MkDocsNav:
    """Parsed mkdocs navigation structure."""

    root: "MkDocsNavNode"
    by_path: dict[str, "MkDocsNavNode"] = field(default_factory=dict)
    by_section: dict[str, list["MkDocsNavNode"]] = field(default_factory=dict)

    def get_node_for_path(self, doc_path: str) -> Optional["MkDocsNavNode"]:
        """Find the nav node for a given doc path."""
        # Try exact match
        if doc_path in self.by_path:
            return self.by_path[doc_path]

        # Try with .md suffix
        if not doc_path.endswith(".md"):
            with_md = f"{doc_path}.md"
            if with_md in self.by_path:
                return self.by_path[with_md]

        # Try without .md suffix
        if doc_path.endswith(".md"):
            without_md = doc_path[:-3]
            if without_md in self.by_path:
                return self.by_path[without_md]

        return None

    def get_docs_in_section(self, section: str) -> list["MkDocsNavNode"]:
        """Get all docs in a section."""
        return self.by_section.get(section, [])


def parse_mkdocs_nav(mkdocs_path: Path) -> MkDocsNav:
    """
    Parse mkdocs.yml to extract navigation structure.

    Args:
        mkdocs_path: Path to mkdocs.yml file

    Returns:
        MkDocsNav with parsed hierarchy
    """
    if not mkdocs_path.exists():
        log.warning(f"mkdocs.yml not found at {mkdocs_path}")
        return MkDocsNav(root=MkDocsNavNode(title="Root", path=None))

    try:
        with open(mkdocs_path, encoding="utf-8") as f:
            content = f.read()

        # Use safe_load with custom constructor for python/name tags
        # Ignore custom tags that we don't need for navigation parsing
        class SafeLoaderIgnoreUnknown(yaml.SafeLoader):
            pass

        def ignore_unknown_tags(loader, tag_suffix, node):
            """Ignore unknown tags like python/name."""
            return None

        # Register constructor for unknown tags
        SafeLoaderIgnoreUnknown.add_multi_constructor(
            "tag:yaml.org,2002:python/name:",
            ignore_unknown_tags,
        )
        SafeLoaderIgnoreUnknown.add_multi_constructor(
            "tag:yaml.org,2002:python/object/apply:",
            ignore_unknown_tags,
        )

        config = yaml.load(content, Loader=SafeLoaderIgnoreUnknown)

    except Exception as e:
        log.error(f"Failed to parse mkdocs.yml: {e}")
        # Fallback: try to parse at least the nav section with regex
        return _parse_mkdocs_fallback(mkdocs_path)

    nav = config.get("nav", {}) if config else {}
    root = MkDocsNavNode(title="Root", path=None)
    index = MkDocsNav(root=root, by_path={}, by_section={})

    _build_nav_tree(nav, root, index, "root")
    _index_by_path(root, index)
    _index_by_section(root, index)

    log.info(f"Parsed mkdocs nav: {len(index.by_path)} pages")
    return index


def _parse_mkdocs_fallback(mkdocs_path: Path) -> MkDocsNav:
    """
    Fallback parser that extracts basic nav structure using regex.

    Used when yaml parsing fails due to custom tags.
    """
    import re

    root = MkDocsNavNode(title="Root", path=None)
    index = MkDocsNav(root=root, by_path={}, by_section={})

    try:
        with open(mkdocs_path, encoding="utf-8") as f:
            content = f.read()

        # Extract nav section (simplified)
        in_nav = False
        indent_level = 0

        for line in content.split("\n"):
            stripped = line.lstrip()
            indent = len(line) - len(stripped)

            if stripped == "nav:":
                in_nav = True
                continue
            elif in_nav and stripped.startswith("-") and ".md:" in stripped:
                # Extract page reference like "- Title: path.md"
                match = re.match(r"-\s+([^:]+):\s*(.+\.md)", stripped)
                if match:
                    title = match.group(1).strip()
                    path = match.group(2).strip()

                    node = MkDocsNavNode(
                        title=title,
                        path=path,
                        parent=None,
                        section="docs",
                    )
                    root.children.append(node)
                    index.by_path[path] = node

        _index_by_section(root, index)

    except Exception as e:
        log.warning(f"Fallback nav parsing also failed: {e}")

    return index


def _build_nav_tree(
    nav_items: Any,
    parent: MkDocsNavNode,
    index: MkDocsNav,
    section: str,
) -> None:
    """Recursively build navigation tree."""
    if not isinstance(nav_items, list):
        return

    for item in nav_items:
        if isinstance(item, str):
            # Simple string: just a path
            node = MkDocsNavNode(
                title=Path(item).stem,
                path=item,
                parent=parent,
                section=section,
            )
            parent.children.append(node)
        elif isinstance(item, dict):
            for key, value in item.items():
                if isinstance(value, str):
                    # Single page: {Title: path.md}
                    node = MkDocsNavNode(
                        title=key,
                        path=value,
                        parent=parent,
                        section=section,
                    )
                    parent.children.append(node)
                elif isinstance(value, list):
                    # Section with children: {Title: [children]}
                    node = MkDocsNavNode(
                        title=key,
                        path=None,  # Section header
                        parent=parent,
                        section=_slugify(key),
                    )
                    parent.children.append(node)
                    _build_nav_tree(value, node, index, node.section)


def _index_by_path(node: MkDocsNavNode, index: MkDocsNav) -> None:
    """Index nodes by their path."""
    if node.path:
        index.by_path[node.path] = node
    for child in node.children:
        _index_by_path(child, index)


def _index_by_section(node: MkDocsNavNode, index: MkDocsNav) -> None:
    """Index nodes by section."""
    if node.path:
        section = node.section or "root"
        if section not in index.by_section:
            index.by_section[section] = []
        index.by_section[section].append(node)
    for child in node.children:
        _index_by_section(child, index)


def _slugify(text: str) -> str:
    """Convert text to slug."""
    return text.lower().replace(" ", "-").replace("/", "-").replace("_", "-")


class DocsIndex:
    """
    Index for documentation with mkdocs hierarchy awareness.

    Provides enhanced search that understands doc structure.
    """

    def __init__(
        self,
        gmas_path: Path,
        mkdocs_nav: MkDocsNav | None = None,
    ):
        """
        Initialize the docs index.

        Args:
            gmas_path: Path to gmas directory
            mkdocs_nav: Optional parsed mkdocs navigation
        """
        self.gmas_path = gmas_path
        self.mkdocs_nav = mkdocs_nav or parse_mkdocs_nav(gmas_path / "mkdocs.yml")
        self.docs: dict[str, SourceDocument] = {}
        self._by_section: dict[str, list[SourceDocument]] = {}

    def index_docs(self, sources: list[SourceDocument]) -> None:
        """Index documentation sources."""
        for source in sources:
            if source.source_type == SourceType.DOCUMENTATION:
                self.docs[source.source_id] = source

                # Categorize by section using mkdocs nav
                rel_path = (
                    source.path.relative_to(self.gmas_path)
                    if self.gmas_path in source.path.parents
                    else source.path
                )
                path_str = str(rel_path).replace("\\", "/")

                node = self.mkdocs_nav.get_node_for_path(path_str)
                if node:
                    section = node.section or "docs"
                    if section not in self._by_section:
                        self._by_section[section] = []
                    self._by_section[section].append(source)

        log.info(
            f"Indexed {len(self.docs)} documents across {len(self._by_section)} sections"
        )

    def get_docs_in_section(self, section: str) -> list[SourceDocument]:
        """Get all documents in a section."""
        return self._by_section.get(section, [])

    def get_doc_hierarchy(self, doc_path: str) -> list[str] | None:
        """Get the navigation hierarchy for a document."""
        node = self.mkdocs_nav.get_node_for_path(doc_path)
        if not node:
            return None

        parts = []
        current = node.parent
        while current and current.title:
            parts.append(current.title)
            current = current.parent
        parts.reverse()
        if node.title:
            parts.append(node.title)
        return parts

    def get_related_docs(self, doc_path: str) -> list[SourceDocument]:
        """Get related docs in the same section."""
        node = self.mkdocs_nav.get_node_for_path(doc_path)
        if not node:
            return []

        section = node.section or "docs"
        return self.get_docs_in_section(section)
