import json
from dataclasses import dataclass
from pathlib import Path

from umbrella.discovery.external_catalog import (
    catalog_path,
    find_by_storage_ref,
    register_card,
    resolve_ref,
)
from umbrella.discovery.web_page_chunks import preview_text, split_sections


@dataclass
class _Ctx:
    drive_root: Path


def test_register_and_resolve(tmp_path: Path) -> None:
    drive = tmp_path / "ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = _Ctx(drive_root=drive)
    cid = register_card(
        ctx,
        kind="github_snippet",
        source_id="github:acme/demo/main.py",
        storage_ref=".memory/drive/memory/knowledge/inspiration/acme/demo/main.py.md",
        preview="class Demo: pass",
    )
    assert cid.startswith("ek:")
    card = resolve_ref(ctx, cid)
    assert card is not None
    assert card["source_id"] == "github:acme/demo/main.py"
    assert find_by_storage_ref(
        ctx, ".memory/drive/memory/knowledge/inspiration/acme/demo/main.py.md"
    ) == cid
    path = catalog_path(ctx)
    assert path is not None and path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["cards"]) == 1


def test_split_sections_markdown() -> None:
    text = "## Intro\nhello\n## API\nendpoints\n"
    parts = split_sections(text)
    assert len(parts) >= 2
    assert parts[0][0] == "Intro"


def test_preview_truncates() -> None:
    long = "word " * 200
    assert len(preview_text(long)) <= 404
