"""Lightweight previews for workspace files.

The live agent often needs a quick look at task artifacts such as `.docx`
or `.pptx` without shelling out to external converters. This module keeps
that path in-process and dependency-free so both Umbrella and Ouroboros can
surface readable previews instead of ZIP binary noise.
"""

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _paragraph_text(node: ET.Element) -> str:
    pieces: list[str] = []
    for child in node.iter():
        local = _local_name(child.tag)
        if local == "t" and child.text:
            pieces.append(child.text)
        elif local == "tab":
            pieces.append("\t")
        elif local in {"br", "cr"}:
            pieces.append("\n")
    return "".join(pieces).strip()


def _extract_xml_text(payload: bytes) -> str:
    root = ET.fromstring(payload)
    blocks: list[str] = []
    for node in root.iter():
        if _local_name(node.tag) == "p":
            text = _paragraph_text(node)
            if text:
                blocks.append(text)
    if blocks:
        return "\n".join(blocks).strip()

    fallback: list[str] = []
    for node in root.iter():
        if _local_name(node.tag) == "t" and node.text:
            fallback.append(node.text)
    return "\n".join(part.strip() for part in fallback if part.strip()).strip()


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    limit = max(1000, min(int(max_chars), 100000))
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _read_docx_preview(path: Path, max_chars: int) -> tuple[str, bool, str]:
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name
            for name in archive.namelist()
            if name == "word/document.xml"
            or (name.startswith("word/header") and name.endswith(".xml"))
            or (name.startswith("word/footer") and name.endswith(".xml"))
        )
        for name in names:
            text = _extract_xml_text(archive.read(name))
            if text:
                parts.append(text)
    content = "\n\n".join(part for part in parts if part).strip() or "[empty docx]"
    preview, truncated = _truncate(content, max_chars)
    return preview, truncated, "office_docx"


def _read_pptx_preview(path: Path, max_chars: int) -> tuple[str, bool, str]:
    slides: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        for idx, name in enumerate(names, 1):
            text = _extract_xml_text(archive.read(name))
            if text:
                slides.append(f"[slide {idx}]\n{text}")
    content = "\n\n".join(slides).strip() or "[empty pptx]"
    preview, truncated = _truncate(content, max_chars)
    return preview, truncated, "office_pptx"


def read_file_preview(path: Path, max_chars: int = 30000) -> tuple[str, bool, str]:
    """Return `(content, truncated, kind)` for a workspace file preview."""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix == ".docx":
        return _read_docx_preview(target, max_chars)
    if suffix == ".pptx":
        return _read_pptx_preview(target, max_chars)

    raw = target.read_bytes()
    if b"\x00" in raw[:8192]:
        return "[binary file preview unavailable]", False, "binary"

    text = raw.decode("utf-8", errors="replace")
    preview, truncated = _truncate(text, max_chars)
    return preview, truncated, "text"
