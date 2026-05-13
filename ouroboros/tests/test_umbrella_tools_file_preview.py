import json
import zipfile
from pathlib import Path

from ouroboros.tools import umbrella_tools


class _FakeCtx:
    def __init__(self, repo_root: Path, drive_root: Path) -> None:
        self.repo_dir = repo_root
        self.host_repo_root = repo_root
        self.drive_root = drive_root


def _write_minimal_docx(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "word/document.xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>""",
        )


def test_read_workspace_file_previews_docx(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    drive_root = tmp_path / "drive"
    workspace_root = repo_root / "workspaces" / "demo"
    workspace_root.mkdir(parents=True)
    (repo_root / "umbrella").mkdir()
    drive_root.mkdir(parents=True)
    _write_minimal_docx(workspace_root / "brief.docx", "GMAS preview text")
    ctx = _FakeCtx(repo_root, drive_root)

    raw = umbrella_tools.read_workspace_file(
        ctx,
        workspace_id="demo",
        file_path="brief.docx",
        max_chars=4000,
    )

    payload = json.loads(raw)
    assert payload["content_kind"] == "office_docx"
    assert "GMAS preview text" in payload["content"]
    assert "PK" not in payload["content"]


def _make_workspace(tmp_path: Path, name: str = "demo") -> tuple[_FakeCtx, Path]:
    repo_root = tmp_path / "repo"
    drive_root = tmp_path / "drive"
    workspace_root = repo_root / "workspaces" / name
    workspace_root.mkdir(parents=True)
    (repo_root / "umbrella").mkdir()
    drive_root.mkdir(parents=True)
    return _FakeCtx(repo_root, drive_root), workspace_root


def test_read_workspace_file_coerces_string_max_chars(tmp_path: Path) -> None:
    """LLMs occasionally pass numeric args as strings; the tool must
    not crash with `TypeError` when ``max_chars`` arrives as ``"4000"``."""
    ctx, ws = _make_workspace(tmp_path)
    (ws / "notes.txt").write_text("hello world", encoding="utf-8")

    raw = umbrella_tools.read_workspace_file(
        ctx,
        workspace_id="demo",
        file_path="notes.txt",
        max_chars="4000",
    )
    payload = json.loads(raw)
    assert payload["content"].strip() == "hello world"


def test_list_workspace_files_coerces_string_max_entries(tmp_path: Path) -> None:
    ctx, ws = _make_workspace(tmp_path)
    for i in range(3):
        (ws / f"f{i}.txt").write_text(str(i), encoding="utf-8")

    raw = umbrella_tools.list_workspace_files(
        ctx,
        workspace_id="demo",
        subdir="",
        max_entries="50",
    )
    payload = json.loads(raw)
    assert len(payload["entries"]) == 3
    assert "...(truncated)" not in payload["entries"]


def test_read_workspace_file_resolves_nfd_filename_via_nfc_lookup(
    tmp_path: Path,
) -> None:
    """macOS-saved files land on disk in NFD form; LLM input is NFC.
    The resolver must reconcile the two without bleeding into the
    user's caller-provided string."""
    import unicodedata

    ctx, ws = _make_workspace(tmp_path)
    nfc_name = "пайплайн_автоматические_карточки.txt"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    assert nfc_name != nfd_name, "test inputs must be in different unicode forms"
    (ws / nfd_name).write_text("ru content", encoding="utf-8")

    raw = umbrella_tools.read_workspace_file(
        ctx,
        workspace_id="demo",
        file_path=nfc_name,
        max_chars=4000,
    )
    payload = json.loads(raw)
    assert payload["content"].strip() == "ru content"
    assert "not found" not in raw.lower()


def test_read_workspace_file_missing_file_returns_helpful_hint(
    tmp_path: Path,
) -> None:
    ctx, _ = _make_workspace(tmp_path)
    out = umbrella_tools.read_workspace_file(
        ctx,
        workspace_id="demo",
        file_path="does_not_exist.docx",
    )
    assert "not found" in out.lower()
    assert "list_workspace_files" in out
