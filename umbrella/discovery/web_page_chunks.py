"""Split fetched web text into page + section artifacts for the external catalog."""

import re
from pathlib import Path
from urllib.parse import urlparse

_MAX_SECTIONS = 12
_SECTION_CHARS = 6000
_PREVIEW_CHARS = 400


def slugify(value: str, *, max_len: int = 80) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "_", (value or "").strip().lower())
    text = text.strip("._-") or "untitled"
    return text[:max_len]


def canonical_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return (url or "").strip()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def page_paths_for_url(url: str) -> tuple[str, str, Path]:
    """Return (host_slug, page_slug, relative dir under knowledge/web/pages/)."""
    parsed = urlparse(canonical_url(url))
    host = slugify(parsed.netloc or "unknown")
    path_part = parsed.path.strip("/") or "index"
    page_slug = slugify(path_part.replace("/", "_"))
    rel_dir = Path("memory") / "knowledge" / "web" / "pages" / host / page_slug
    return host, page_slug, rel_dir


def split_sections(text: str) -> list[tuple[str, str]]:
    """Return (heading, body) chunks from markdown ## or HTML h1-h3."""
    raw = (text or "").strip()
    if not raw:
        return []
    if "## " in raw:
        parts = re.split(r"(?m)^##\s+(.+)$", raw)
        if len(parts) > 1:
            out: list[tuple[str, str]] = []
            if parts[0].strip():
                out.append(("Introduction", parts[0].strip()))
            for idx in range(1, len(parts) - 1, 2):
                heading = parts[idx].strip()
                body = parts[idx + 1].strip() if idx + 1 < len(parts) else ""
                if heading and body:
                    out.append((heading, body[:_SECTION_CHARS]))
            return out[:_MAX_SECTIONS]
    html_heads = list(
        re.finditer(
            r"<h([1-3])[^>]*>(.*?)</h\1>",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    if html_heads:
        out = []
        for idx, match in enumerate(html_heads[:_MAX_SECTIONS]):
            heading = re.sub(r"<[^>]+>", " ", match.group(2))
            heading = re.sub(r"\s+", " ", heading).strip() or f"Section {idx + 1}"
            start = match.end()
            end = html_heads[idx + 1].start() if idx + 1 < len(html_heads) else len(raw)
            body = re.sub(r"<[^>]+>", " ", raw[start:end])
            body = re.sub(r"\s+", " ", body).strip()[:_SECTION_CHARS]
            if body:
                out.append((heading, body))
        if out:
            return out
    if len(raw) > _SECTION_CHARS:
        return [("Full page (part 1)", raw[:_SECTION_CHARS])]
    return [("Full page", raw)]


def preview_text(text: str, *, limit: int = _PREVIEW_CHARS) -> str:
    one_line = re.sub(r"\s+", " ", (text or "").strip())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 3].rstrip() + "..."
