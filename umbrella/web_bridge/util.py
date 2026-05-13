import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_BUILD_DIR = REPO_ROOT / "web" / "build"
WEB_DIST_DIR = REPO_ROOT / "web" / "dist"
WEB_STORE_DIR = REPO_ROOT / ".umbrella" / "web"

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_MODELS = [
    {
        "id": "gpt-4o-mini",
        "name": "GPT-4o mini",
        "provider": "openai",
        "context": 128000,
    },
    {"id": "gpt-4o", "name": "GPT-4o", "provider": "openai", "context": 128000},
    {
        "id": "claude-3.5-sonnet",
        "name": "Claude 3.5 Sonnet",
        "provider": "anthropic",
        "context": 200000,
    },
    {
        "id": "umbrella-7b",
        "name": "Umbrella 7B (local)",
        "provider": "umbrella",
        "context": 32000,
    },
]

# First entry in ``DEFAULT_MODELS`` (historical web UI default before env alignment).
_LEGACY_WEB_DEFAULT_MODEL_ID = DEFAULT_MODELS[0]["id"]


def resolve_default_ouroboros_model() -> str:
    """Default chat/Ouroboros model: repo ``.env`` (``OUROBOROS_MODEL`` / ``LLM_MODEL``) then catalog."""
    return (
        os.environ.get("OUROBOROS_MODEL", "").strip()
        or os.environ.get("LLM_MODEL", "").strip()
        or _LEGACY_WEB_DEFAULT_MODEL_ID
    )


DEFAULT_TOOLS = [
    {
        "id": "web_search",
        "name": "Web Search",
        "desc": "Search the web for real-time information",
    },
    {"id": "python", "name": "Python", "desc": "Execute Python code"},
    {"id": "db_query", "name": "Database Query", "desc": "Run database queries"},
    {"id": "file_read", "name": "File Read", "desc": "Read files from workspace"},
    {"id": "api_call", "name": "API Call", "desc": "Make HTTP API calls"},
]


def now_ts() -> float:
    return time.time()


def iso_utc(ts: float | None = None) -> str:
    if ts is None:
        ts = now_ts()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + "Z"


def json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


def read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    if limit is not None and limit > 0:
        lines = lines[-limit:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def ensure_store() -> None:
    WEB_STORE_DIR.mkdir(parents=True, exist_ok=True)


def store_path(name: str) -> Path:
    ensure_store()
    return WEB_STORE_DIR / name


def load_store(name: str, default: Any) -> Any:
    path = store_path(name)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_store(name: str, value: Any) -> None:
    store_path(name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def short_text(text: str, n: int = 160) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def slug_workspace_name(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()).strip("_").lower()
    return s or f"ws_{uuid.uuid4().hex[:8]}"
