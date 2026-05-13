"""Tests for ``umbrella.utils.file_probe.probe_file``."""

import io
import json
import zipfile
from pathlib import Path

from umbrella.utils.file_probe import ProbeResult, probe_file


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_probe_detects_plain_text_masquerading_as_docx(tmp_path: Path) -> None:
    fake_docx = _write(
        tmp_path / "requirements.docx",
        b"This is just plain text saved with a .docx extension.",
    )
    result = probe_file(fake_docx)
    assert isinstance(result, ProbeResult)
    assert result.exists is True
    assert result.declared_ext == ".docx"
    assert result.actual_format == "utf-8 text"
    assert result.mismatch is True
    assert "python-docx" in result.hint or "PackageNotFoundError" in result.hint


def test_probe_detects_genuine_docx_zip(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<doc/>")
        zf.writestr("[Content_Types].xml", "<types/>")
    real_docx = _write(tmp_path / "real.docx", buf.getvalue())
    result = probe_file(real_docx)
    assert result.actual_format == "docx"
    assert result.mismatch is False
    assert result.hint == ""


def test_probe_detects_pdf(tmp_path: Path) -> None:
    pdf = _write(tmp_path / "doc.pdf", b"%PDF-1.4\n%test\n")
    result = probe_file(pdf)
    assert result.actual_format == "pdf"
    assert result.mismatch is False


def test_probe_flags_text_disguised_as_pdf(tmp_path: Path) -> None:
    fake = _write(tmp_path / "doc.pdf", b"Hello world, not a PDF.\n")
    result = probe_file(fake)
    assert result.actual_format == "utf-8 text"
    assert result.mismatch is True
    assert "PDF" in result.hint or "UTF-8" in result.hint


def test_probe_detects_json(tmp_path: Path) -> None:
    payload = json.dumps({"hello": "world"}).encode("utf-8")
    f = _write(tmp_path / "blob.json", payload)
    result = probe_file(f)
    assert result.actual_format == "json"
    assert result.mismatch is False


def test_probe_flags_text_disguised_as_image(tmp_path: Path) -> None:
    fake = _write(tmp_path / "logo.png", b"not really a png\n")
    result = probe_file(fake)
    assert result.actual_format == "utf-8 text"
    assert result.mismatch is True
    assert (
        "image" in result.hint.lower()
        or "PIL" in result.hint
        or "Pillow" in result.hint
    )


def test_probe_handles_missing_file(tmp_path: Path) -> None:
    result = probe_file(tmp_path / "nope.txt")
    assert result.exists is False
    assert result.actual_format == "missing"
    assert result.mismatch is False
    assert "exist" in result.hint.lower()


def test_probe_handles_empty_file(tmp_path: Path) -> None:
    f = _write(tmp_path / "empty.txt", b"")
    result = probe_file(f)
    assert result.exists is True
    assert result.size_bytes == 0
    assert result.actual_format == "empty"
    # ``.txt`` is in the text family but ``empty`` isn't — that's fine,
    # we don't flag empty as a mismatch.
    assert result.mismatch is False


def test_probe_detects_real_png(tmp_path: Path) -> None:
    png_magic = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    f = _write(tmp_path / "img.png", png_magic)
    result = probe_file(f)
    assert result.actual_format == "png"
    assert result.mismatch is False


def test_probe_csv_with_commas(tmp_path: Path) -> None:
    f = _write(tmp_path / "data.csv", b"a,b,c\n1,2,3\n")
    result = probe_file(f)
    assert result.actual_format == "csv"
    assert result.mismatch is False
    assert result.details.get("csv_columns") == 3


def test_probe_returns_jsonable_dict(tmp_path: Path) -> None:
    f = _write(tmp_path / "x.txt", b"hello")
    result = probe_file(f)
    payload = result.to_dict()
    json.dumps(payload)  # must not raise
    assert payload["path"].endswith("x.txt")
    assert payload["actual_format"] == "utf-8 text"
