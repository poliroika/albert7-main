import os
import pathlib
from typing import Any

from umbrella.memory.palace.graph import GraphStore
from umbrella.memory.palace.transient import TransientStore

_CHROMA_STORES = [
    "palace.charter",
    "palace.lesson",
    "palace.idea",
    "palace.codeptr",
    "palace.skill_index",
    "palace.run",
    "palace.phase",
    "palace.subtask",
    "palace.durable",
]


def _chroma_collection_name(store: str) -> str:
    return store.replace(".", "_")


class PalaceStores:
    def __init__(self, palace_root: pathlib.Path) -> None:
        self._root = palace_root
        self._root.mkdir(parents=True, exist_ok=True)
        self._graph = GraphStore(palace_root / "graph.sqlite")
        self._transient = TransientStore(
            palace_root / "transient.sqlite",
            default_ttl_seconds=int(os.environ.get("OUROBOROS_PALACE_TRANSIENT_TTL_SEC", "86400")),
        )
        self._chroma_clients: dict[str, Any] = {}

    def chroma(self, store: str) -> Any:
        if store not in self._chroma_clients:
            self._chroma_clients[store] = self._open_chroma(store)
        return self._chroma_clients[store]

    def _open_chroma(self, store: str) -> Any:
        try:
            import chromadb
        except ImportError as exc:
            if os.environ.get("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB") == "1":
                return _NullChromaCollection(f"{self._root}:{store}")
            raise RuntimeError(
                "chromadb is not installed; persistent Palace memory is unavailable. "
                "Install chromadb or set UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB=1 for tests only."
            ) from exc
        collection_dir = self._root / _chroma_collection_name(store)
        collection_dir.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(collection_dir))
        return client.get_or_create_collection(
            name=_chroma_collection_name(store),
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def graph(self) -> GraphStore:
        return self._graph

    @property
    def transient(self) -> TransientStore:
        return self._transient

    def health(self) -> dict[str, Any]:
        ok_stores = []
        bad_stores = []
        volatile_stub = os.environ.get("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB") == "1"
        for s in _CHROMA_STORES:
            try:
                col = self.chroma(s)
                if isinstance(col, _NullChromaCollection):
                    if not volatile_stub:
                        bad_stores.append(f"{s}: volatile in-memory stub (chromadb missing)")
                    else:
                        ok_stores.append(f"{s}:volatile_stub")
                else:
                    col.count()
                    ok_stores.append(s)
            except Exception as exc:
                bad_stores.append(f"{s}: {exc}")
        return {
            "ok": not bad_stores,
            "stores_ok": ok_stores,
            "stores_fail": bad_stores,
            "volatile_stub": volatile_stub,
        }

    def close(self) -> None:
        for collection in list(self._chroma_clients.values()):
            client = getattr(collection, "_client", None)
            try:
                if client is not None and hasattr(client, "close"):
                    client.close()
            except Exception:
                pass
            try:
                del client
            except UnboundLocalError:
                pass
            try:
                del collection
            except UnboundLocalError:
                pass
        self._chroma_clients.clear()
        try:
            from chromadb.api.shared_system_client import SharedSystemClient

            SharedSystemClient._identifier_to_system.clear()
        except Exception:
            pass
        try:
            import gc

            gc.collect()
        except Exception:
            pass
        self._graph.close()
        self._transient.close()


class _NullChromaCollection:
    """Process-global stub when chromadb is not installed (tests only)."""

    _GLOBAL_ITEMS: dict[str, list[dict]] = {}

    def __init__(self, key: str) -> None:
        self._key = key
        if key not in self._GLOBAL_ITEMS:
            self._GLOBAL_ITEMS[key] = []

    @property
    def _items(self) -> list[dict]:
        return self._GLOBAL_ITEMS[self._key]

    def count(self) -> int:
        return len(self._items)

    def add(self, *, ids, documents, metadatas=None, embeddings=None) -> None:
        for i, (id_, doc) in enumerate(zip(ids, documents)):
            self._items.append({"id": id_, "document": doc, "metadata": (metadatas or [{}])[i]})

    def query(self, query_texts=None, n_results=10, where=None, **kwargs) -> dict:
        items = self._items
        if where:
            items = [it for it in items if all(it["metadata"].get(k) == v for k, v in where.items())]
        sliced = items[:n_results]
        return {
            "ids": [[it["id"] for it in sliced]],
            "documents": [[it["document"] for it in sliced]],
            "metadatas": [[it["metadata"] for it in sliced]],
            "distances": [[0.5] * len(sliced)],
        }

    def get(self, ids=None, where=None, **kwargs) -> dict:
        items = self._items
        if ids:
            items = [it for it in items if it["id"] in ids]
        if where:
            items = [it for it in items if all(it["metadata"].get(k) == v for k, v in where.items())]
        return {"ids": [it["id"] for it in items], "documents": [it["document"] for it in items], "metadatas": [it["metadata"] for it in items]}

    def delete(self, ids=None, where=None, **kwargs) -> None:
        if ids:
            self._items = [it for it in self._items if it["id"] not in ids]
        elif where:
            self._items = [it for it in self._items if not all(it["metadata"].get(k) == v for k, v in where.items())]
