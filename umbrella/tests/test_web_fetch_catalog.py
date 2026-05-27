import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

from umbrella.deep_agent_tools.workspace_ops import web_fetch


@dataclass
class _Ctx:
    drive_root: Path
    repo_dir: Path


def test_web_fetch_registers_catalog(tmp_path: Path) -> None:
    ws = tmp_path / "workspaces" / "demo"
    drive = ws / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = _Ctx(drive_root=drive, repo_dir=ws)
    html = "<html><body><h1>Title</h1><p>Hello world</p></body></html>"
    mock_resp = MagicMock()
    mock_resp.url = "https://example.com/docs"
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/html"}
    mock_resp.text = html
    with patch("httpx.get", return_value=mock_resp):
        out = json.loads(web_fetch(ctx, url="https://example.com/docs", include_content=False))
    assert out.get("catalog_id", "").startswith("ek:")
    assert out.get("section_ids")
    page = drive / "memory" / "knowledge" / "web" / "pages" / "example.com" / "docs" / "index.md"
    assert page.is_file()
