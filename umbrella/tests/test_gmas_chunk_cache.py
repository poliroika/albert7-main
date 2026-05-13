"""Palace-backed GMAS chunk cache keys."""

from unittest.mock import MagicMock, patch

from umbrella.retrieval.gmas_chunk_cache import (
    get_cached_summary,
    make_cache_key,
    put_cached_summary,
)


def test_cache_roundtrip_key_stable() -> None:
    k = make_cache_key("gmas/src/x.py", "abcd1234", 800)
    assert "gmas_chunk::" in k
    assert "abcd1234" in k


def test_get_returns_cached_when_palace_hits() -> None:
    with patch("umbrella.memory.palace_backend.get_palace_backend") as gp:
        palace = MagicMock()
        palace.fetch_document_by_metadata.return_value = "[t]\nBODY"
        gp.return_value = palace
        out = get_cached_summary(__import__("pathlib").Path("/repo"), "k1")
        assert "BODY" in out


def test_put_calls_add() -> None:
    with patch("umbrella.memory.palace_backend.get_palace_backend") as gp:
        palace = MagicMock()
        gp.return_value = palace
        put_cached_summary(
            __import__("pathlib").Path("/repo"), "k2", "sumtext", "gmas/a.py"
        )
        palace.add.assert_called_once()
        call_kw = palace.add.call_args.kwargs
        assert call_kw.get("room") == "gmas_chunks"
        assert call_kw.get("workspace_id") == "__shared__"
