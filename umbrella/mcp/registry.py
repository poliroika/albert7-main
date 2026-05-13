"""On-disk MCP server registry.

The registry is stored at ``.umbrella/mcp/registry.json`` so it persists
across runs and can be mutated by the Web UI without touching the agent.
"""

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from collections.abc import Iterable

__all__ = ["McpRegistry", "McpServerSpec", "default_registry_path"]


VALID_TRANSPORTS = {"stdio", "http", "sse"}
VALID_STATUS = {"enabled", "disabled"}


def default_registry_path(repo_root: Path) -> Path:
    return Path(repo_root) / ".umbrella" / "mcp" / "registry.json"


@dataclass
class McpServerSpec:
    """Persistent description of one MCP server."""

    id: str
    name: str
    transport: str = "stdio"
    command: str = ""  # for stdio
    args: list[str] = field(default_factory=list)
    url: str = ""  # for http/sse
    env: dict[str, str] = field(default_factory=dict)
    status: str = "disabled"
    source: str = "discovered"  # builtin | discovered | user
    description: str = ""
    install_notes: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "McpServerSpec":
        return cls(
            id=str(payload.get("id") or uuid.uuid4().hex[:10]),
            name=str(payload.get("name") or ""),
            transport=str(payload.get("transport") or "stdio"),
            command=str(payload.get("command") or ""),
            args=list(payload.get("args") or []),
            url=str(payload.get("url") or ""),
            env=dict(payload.get("env") or {}),
            status=str(payload.get("status") or "disabled"),
            source=str(payload.get("source") or "discovered"),
            description=str(payload.get("description") or ""),
            install_notes=str(payload.get("install_notes") or ""),
            created_at=float(payload.get("created_at") or 0.0),
            updated_at=float(payload.get("updated_at") or 0.0),
        )

    def validate(self) -> str:
        if not self.name.strip():
            return "name is required"
        if self.transport not in VALID_TRANSPORTS:
            return f"transport must be one of {sorted(VALID_TRANSPORTS)}"
        if self.transport == "stdio" and not self.command.strip():
            return "stdio transport requires a command"
        if self.transport in {"http", "sse"} and not self.url.strip():
            return f"{self.transport} transport requires a url"
        if self.status not in VALID_STATUS:
            return f"status must be one of {sorted(VALID_STATUS)}"
        return ""


class McpRegistry:
    """File-backed registry of MCP server specs."""

    def __init__(self, repo_root: Path, *, path: Path | None = None) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.path = path or default_registry_path(self.repo_root)
        self._lock = threading.Lock()

    def _read_raw(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            servers = payload.get("servers")
            if isinstance(servers, list):
                return [item for item in servers if isinstance(item, dict)]
        return []

    def _write_raw(self, items: Iterable[dict[str, Any]]) -> None:
        payload = {"servers": list(items), "updated_at": time.time()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    def list_servers(self) -> list[McpServerSpec]:
        with self._lock:
            return [McpServerSpec.from_dict(item) for item in self._read_raw()]

    def get(self, server_id: str) -> McpServerSpec | None:
        for spec in self.list_servers():
            if spec.id == server_id:
                return spec
        return None

    def upsert(self, spec: McpServerSpec) -> McpServerSpec:
        err = spec.validate()
        if err:
            raise ValueError(err)
        with self._lock:
            items = self._read_raw()
            now = time.time()
            spec.updated_at = now
            if not spec.created_at:
                spec.created_at = now
            replaced = False
            new_items: list[dict[str, Any]] = []
            for item in items:
                if str(item.get("id") or "") == spec.id:
                    new_items.append(spec.to_dict())
                    replaced = True
                else:
                    new_items.append(item)
            if not replaced:
                new_items.append(spec.to_dict())
            self._write_raw(new_items)
        return spec

    def add_new(
        self,
        *,
        name: str,
        transport: str,
        command: str = "",
        args: list[str] | None = None,
        url: str = "",
        env: dict[str, str] | None = None,
        source: str = "discovered",
        description: str = "",
        install_notes: str = "",
        status: str = "disabled",
    ) -> McpServerSpec:
        spec = McpServerSpec(
            id=uuid.uuid4().hex[:10],
            name=name.strip(),
            transport=transport,
            command=command,
            args=list(args or []),
            url=url,
            env=dict(env or {}),
            source=source,
            description=description,
            install_notes=install_notes,
            status=status,
        )
        return self.upsert(spec)

    def delete(self, server_id: str) -> bool:
        with self._lock:
            items = self._read_raw()
            kept = [item for item in items if str(item.get("id") or "") != server_id]
            if len(kept) == len(items):
                return False
            self._write_raw(kept)
            return True

    def set_status(self, server_id: str, status: str) -> McpServerSpec | None:
        if status not in VALID_STATUS:
            raise ValueError(f"status must be one of {sorted(VALID_STATUS)}")
        spec = self.get(server_id)
        if spec is None:
            return None
        spec.status = status
        spec.updated_at = time.time()
        return self.upsert(spec)
