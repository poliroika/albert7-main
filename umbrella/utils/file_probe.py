"""Magic-bytes / content-shape probe for workspace input files.

Why
---
The agent sometimes accepts a file at face value because of its
extension and then crashes when the actual bytes don't match — e.g. a
``.docx`` that's really a UTF-8 text dump, a ``.xlsx`` that's a CSV
renamed, an "image" that's actually JSON metadata. ``probe_file``
detects this mismatch and returns a structured hint so the agent can
swap to the right parser instead of silently producing empty
extractions.

Universal — no per-workspace heuristics. Detection is based purely on:
1. Leading bytes (magic numbers) for binary formats (zip, gzip, PDF,
   PNG, JPEG, GIF, ELF).
2. UTF-8 / ASCII text shape (printable ratio, BOM).
3. A light CSV vs JSON heuristic for ``.csv`` / ``.json``.

The probe is read-only and bounded (max 64 KB read) so it stays cheap.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PROBE_BYTES: int = 65536

# Mapping from sniffed format to the file extensions that "match" it.
_FORMAT_EXTS: dict[str, frozenset[str]] = {
    "zip-archive": frozenset({".zip"}),
    "docx": frozenset({".docx"}),
    "xlsx": frozenset({".xlsx"}),
    "pptx": frozenset({".pptx"}),
    "pdf": frozenset({".pdf"}),
    "png": frozenset({".png"}),
    "jpeg": frozenset({".jpg", ".jpeg"}),
    "gif": frozenset({".gif"}),
    "gzip": frozenset({".gz", ".tgz"}),
    "elf": frozenset(),
    "utf-8 text": frozenset(
        {
            ".txt",
            ".md",
            ".csv",
            ".tsv",
            ".log",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".ini",
            ".cfg",
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".html",
            ".css",
            ".sh",
            ".bat",
            ".ps1",
            ".rst",
        }
    ),
    "json": frozenset({".json"}),
    "csv": frozenset({".csv", ".tsv"}),
    "binary": frozenset(),
    "empty": frozenset(),
}


@dataclass(slots=True)
class ProbeResult:
    """Structured probe outcome.

    Fields are intentionally JSON-serialisable so the tool wrapper can
    return ``result.to_dict()`` straight to the LLM.
    """

    path: str
    exists: bool
    size_bytes: int
    declared_ext: str
    actual_format: str
    mismatch: bool
    hint: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "declared_ext": self.declared_ext,
            "actual_format": self.actual_format,
            "mismatch": self.mismatch,
            "hint": self.hint,
            "details": dict(self.details),
        }


def _sniff_magic(head: bytes) -> str | None:
    """Return a magic-bytes format label, or ``None`` if no signature matched."""

    if not head:
        return "empty"
    # Zip family (also covers docx/xlsx/pptx — we disambiguate later).
    if (
        head.startswith(b"PK\x03\x04")
        or head.startswith(b"PK\x05\x06")
        or head.startswith(b"PK\x07\x08")
    ):
        return "zip-archive"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"\x7fELF"):
        return "elf"
    return None


def _looks_like_text(head: bytes) -> bool:
    """Return True if ``head`` decodes cleanly as UTF-8 / has mostly printable bytes."""

    if not head:
        return False
    # Strip a UTF-8 BOM if present.
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for b in head if 9 <= b <= 13 or 32 <= b <= 126 or b >= 128)
    return printable >= len(head) * 0.85


def _disambiguate_zip(path: Path) -> str:
    """Look inside a zip to see if it's actually an Office document."""

    try:
        import zipfile

        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "ppt/presentation.xml" in names:
            return "pptx"
    except Exception as exc:  # noqa: BLE001 — defensive: any zip error fallthroughs
        log.debug("Zip introspection failed for %s: %s", path, exc)
    return "zip-archive"


def _disambiguate_text(head: bytes, declared_ext: str) -> tuple[str, dict[str, Any]]:
    """Refine a "text" hit with a CSV/JSON sniff when the extension hints at it."""

    text_head = head.decode("utf-8", errors="replace").lstrip("\ufeff").lstrip()
    details: dict[str, Any] = {"sample_chars": min(len(text_head), 200)}
    if text_head.startswith(("{", "[")):
        try:
            json.loads(text_head[: min(len(text_head), 4096)])
            return "json", details
        except Exception:
            # Truncated JSON often won't parse but still LOOKS like JSON.
            return ("json" if declared_ext == ".json" else "utf-8 text"), details
    if declared_ext in {".csv", ".tsv"} and "\n" in text_head:
        first_line = text_head.split("\n", 1)[0]
        sep = "," if first_line.count(",") >= first_line.count("\t") else "\t"
        cols = first_line.count(sep) + 1
        if cols >= 2:
            details["csv_columns"] = cols
            details["csv_separator"] = sep
            return "csv", details
    return "utf-8 text", details


def _build_hint(declared_ext: str, actual_format: str, mismatch: bool) -> str:
    if not mismatch:
        return ""
    if declared_ext in {".docx", ".doc"} and actual_format == "utf-8 text":
        return (
            "Declared as a Word document but the bytes are plain UTF-8 text. "
            "Open it with `Path.read_text(encoding='utf-8')` instead of "
            "`python-docx` / `Document(...)` — the docx parser will raise "
            "PackageNotFoundError on plain text."
        )
    if declared_ext in {".xlsx", ".xls"} and actual_format in {"utf-8 text", "csv"}:
        return (
            "Declared as an Excel workbook but the bytes are plain text/CSV. "
            "Use `csv.reader` or `pandas.read_csv` instead of `openpyxl` / "
            "`pandas.read_excel`."
        )
    if declared_ext == ".pdf" and actual_format == "utf-8 text":
        return (
            "Declared as a PDF but the bytes are plain text. Read it as "
            "UTF-8; do not invoke pdfminer / PyPDF2 — they will fail."
        )
    if declared_ext in {".png", ".jpg", ".jpeg", ".gif"} and actual_format in {
        "utf-8 text",
        "json",
    }:
        return (
            f"Declared as an image but the bytes look like {actual_format}. "
            "Treat this as metadata/text — do not pass it to PIL/Pillow."
        )
    if declared_ext == ".json" and actual_format == "utf-8 text":
        return (
            "Declared as JSON but the bytes do not parse as JSON. Inspect "
            "the head with `read_workspace_file` before relying on it."
        )
    if declared_ext == ".csv" and actual_format == "utf-8 text":
        return (
            "Declared as CSV but no comma/tab delimiter was detected on the "
            "first line. Verify the separator before calling `csv.reader`."
        )
    return (
        f"File extension {declared_ext!r} disagrees with detected format "
        f"{actual_format!r}. Pick a parser that matches the actual format."
    )


def _classify(path: Path, head: bytes, declared_ext: str) -> tuple[str, dict[str, Any]]:
    """Return (actual_format, details) given a sample of bytes."""

    magic = _sniff_magic(head)
    if magic == "zip-archive":
        refined = _disambiguate_zip(path)
        return refined, {"magic": "zip"}
    if magic is not None:
        return magic, {"magic": magic}
    if _looks_like_text(head):
        return _disambiguate_text(head, declared_ext)
    return "binary", {"magic": None}


def probe_file(path: Path | str) -> ProbeResult:
    """Probe ``path`` and return a :class:`ProbeResult`.

    Never raises — missing files / IO errors return a result with
    ``exists=False`` (or ``actual_format='binary'`` for unreadable
    bytes) and an explanatory ``hint``.
    """

    p = Path(path)
    declared_ext = p.suffix.lower()
    if not p.exists() or not p.is_file():
        return ProbeResult(
            path=str(p),
            exists=False,
            size_bytes=0,
            declared_ext=declared_ext,
            actual_format="missing",
            mismatch=False,
            hint="File does not exist or is not a regular file.",
            details={},
        )

    try:
        size = p.stat().st_size
    except OSError:
        size = -1
    head: bytes = b""
    try:
        with p.open("rb") as fh:
            head = fh.read(_PROBE_BYTES)
    except OSError as exc:
        return ProbeResult(
            path=str(p),
            exists=True,
            size_bytes=size if size >= 0 else 0,
            declared_ext=declared_ext,
            actual_format="binary",
            mismatch=False,
            hint=f"Could not read file bytes: {exc}",
            details={},
        )
    actual_format, details = _classify(p, head, declared_ext)
    matching_exts = _FORMAT_EXTS.get(actual_format, frozenset())
    mismatch = (
        bool(declared_ext) and bool(matching_exts) and declared_ext not in matching_exts
    )
    if not declared_ext:
        # No extension to compare against; only fire a hint if the actual
        # format is binary/empty so the agent doesn't try to read it as
        # text by default.
        mismatch = False
    hint = _build_hint(declared_ext, actual_format, mismatch)
    return ProbeResult(
        path=str(p),
        exists=True,
        size_bytes=size if size >= 0 else len(head),
        declared_ext=declared_ext,
        actual_format=actual_format,
        mismatch=mismatch,
        hint=hint,
        details=details,
    )


__all__ = ["ProbeResult", "probe_file"]
