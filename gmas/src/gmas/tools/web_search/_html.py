"""Simple HTML-to-text parser with an optional fast parser path."""

import html
import re
from typing import ClassVar

try:
    from selectolax.parser import HTMLParser as _SelectolaxHTMLParser
except ImportError:
    _SelectolaxHTMLParser: type | None = None


class SimpleHTMLParser:
    """
    Extract readable text from raw HTML.

    Tries ``<main>``, ``<article>``, ``<div role="main">`` first,
    then falls back to ``<body>`` (or the whole document).
    """

    MIN_USEFUL_CONTENT: ClassVar[int] = 300

    REMOVE_TAGS: ClassVar[set[str]] = {
        "script",
        "style",
        "head",
        "meta",
        "link",
        "noscript",
        "iframe",
        "svg",
        "nav",
        "footer",
        "header",
    }

    BLOCK_TAGS: ClassVar[set[str]] = {
        "p",
        "div",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "tr",
        "article",
        "section",
    }

    _MAIN_CONTENT_PATTERNS: ClassVar[list[tuple[re.Pattern[str], str]]] = [
        (re.compile(r"<main[^>]*>(.*?)</main>", re.IGNORECASE | re.DOTALL), "main"),
        (re.compile(r"<article[^>]*>(.*?)</article>", re.IGNORECASE | re.DOTALL), "article"),
        (re.compile(r"<div[^>]*\brole=['\"]main['\"][^>]*>(.*?)</div>", re.IGNORECASE | re.DOTALL), "div[role=main]"),
    ]
    _TITLE_RE: ClassVar[re.Pattern[str]] = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
    _BODY_RE: ClassVar[re.Pattern[str]] = re.compile(r"<body[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)
    _COMMENT_RE: ClassVar[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)
    _REMOVE_TAGS_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"<(?:script|style|head|meta|link|noscript|iframe|svg|nav|footer|header)\b[^>]*>.*?</(?:script|style|head|meta|link|noscript|iframe|svg|nav|footer|header)>",
        re.IGNORECASE | re.DOTALL,
    )
    _BLOCK_OPEN_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"<(?:p|div|br|h1|h2|h3|h4|h5|h6|li|tr|article|section)\b[^>]*/?>",
        re.IGNORECASE,
    )
    _BLOCK_CLOSE_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"</(?:p|div|br|h1|h2|h3|h4|h5|h6|li|tr|article|section)\s*>",
        re.IGNORECASE,
    )
    _BR_RE: ClassVar[re.Pattern[str]] = re.compile(r"<br\s*/?>", re.IGNORECASE)
    _ANY_TAG_RE: ClassVar[re.Pattern[str]] = re.compile(r"<[^>]+>")
    _SPACE_RE: ClassVar[re.Pattern[str]] = re.compile(r"[ \t]+")
    _MULTI_NL_RE: ClassVar[re.Pattern[str]] = re.compile(r"\n\s*\n+")
    _FAST_MAIN_SELECTOR: ClassVar[str] = "main, article, div[role='main']"
    _FAST_REMOVE_SELECTOR: ClassVar[str] = ",".join(sorted(REMOVE_TAGS))

    @staticmethod
    def _truncate_text(text: str, max_length: int) -> str:
        if len(text) > max_length:
            return text[:max_length] + "\n\n... (content truncated)"
        return text

    @classmethod
    def _extract_title_regex(cls, html_content: str) -> str:
        title_match = cls._TITLE_RE.search(html_content)
        if not title_match:
            return ""
        return html.unescape(title_match.group(1).strip())

    @classmethod
    def _extract_fast_text(cls, html_content: str, *, max_length: int) -> tuple[str, str] | None:
        if _SelectolaxHTMLParser is None:
            return None

        try:
            tree = _SelectolaxHTMLParser(html_content)
            title = ""
            title_node = tree.css_first("title")
            if title_node is not None:
                title = html.unescape(title_node.text(strip=True))

            main_node = tree.css_first(cls._FAST_MAIN_SELECTOR)
            selected_html = (main_node.html or html_content) if main_node is not None else html_content
            text = cls._fast_html_to_text(selected_html, max_length=max_length)

            if len(text) < cls.MIN_USEFUL_CONTENT and main_node is not None:
                body_node = tree.body
                fallback_html = (body_node.html or html_content) if body_node is not None else html_content
                text = cls._fast_html_to_text(fallback_html, max_length=max_length)
        except (AttributeError, TypeError, ValueError):
            return None
        else:
            return title, text

    @classmethod
    def _fast_html_to_text(cls, html_content: str, *, max_length: int) -> str:
        if _SelectolaxHTMLParser is None:
            return cls._stdlib_html_to_text(html_content, max_length=max_length)

        try:
            tree = _SelectolaxHTMLParser(html_content)
            for node in tree.css(cls._FAST_REMOVE_SELECTOR):
                node.decompose()

            root = tree.body or tree.root
            if root is None:
                return ""

            text = root.text(separator="\n", strip=True)
            text = text.replace("\r", "\n").replace("\xa0", " ")
            text = cls._SPACE_RE.sub(" ", text)
            text = cls._MULTI_NL_RE.sub("\n\n", text)
            return cls._truncate_text(text.strip(), max_length)
        except (AttributeError, TypeError, ValueError):
            return cls._stdlib_html_to_text(html_content, max_length=max_length)

    @classmethod
    def _stdlib_html_to_text(cls, html_content: str, *, max_length: int) -> str:
        if not html_content:
            return ""

        text = cls._REMOVE_TAGS_RE.sub(" ", html_content)
        text = cls._COMMENT_RE.sub(" ", text)
        text = cls._BLOCK_CLOSE_RE.sub("\n", text)
        text = cls._BLOCK_OPEN_RE.sub("\n", text)
        text = cls._BR_RE.sub("\n", text)
        text = cls._ANY_TAG_RE.sub(" ", text)
        text = html.unescape(text)
        text = cls._SPACE_RE.sub(" ", text)
        text = cls._MULTI_NL_RE.sub("\n\n", text)
        text = text.strip()
        return cls._truncate_text(text, max_length)

    @classmethod
    def extract_main_content(cls, html_content: str, min_length: int = 500) -> str | None:
        for pattern, _name in cls._MAIN_CONTENT_PATTERNS:
            match = pattern.search(html_content)
            if match and len(match.group(1)) > min_length:
                return match.group(1)
        return None

    @classmethod
    def extract_text(cls, html_content: str, *, max_length: int = 8000) -> tuple[str, str]:
        """Return ``(title, text)`` extracted from raw HTML."""
        fast_result = cls._extract_fast_text(html_content, max_length=max_length)
        if fast_result is not None:
            return fast_result

        title = cls._extract_title_regex(html_content)
        main_content = cls.extract_main_content(html_content) or html_content
        text = cls._stdlib_html_to_text(main_content, max_length=max_length)

        if len(text) < cls.MIN_USEFUL_CONTENT and main_content is not html_content:
            body_match = cls._BODY_RE.search(html_content)
            fallback_html = body_match.group(1) if body_match else html_content
            text = cls._stdlib_html_to_text(fallback_html, max_length=max_length)

        return title, text

    @classmethod
    def html_to_text(cls, html_content: str, max_length: int = 8000) -> str:
        if _SelectolaxHTMLParser is not None:
            return cls._fast_html_to_text(html_content, max_length=max_length)
        return cls._stdlib_html_to_text(html_content, max_length=max_length)
