"""Tests for src/utils/state_storage.py"""

import pytest

from gmas.utils.state_storage import FileStateStorage, InMemoryStateStorage

# ─────────────────────────── InMemoryStateStorage ─────────────────────────────


class TestInMemoryStateStorage:
    def setup_method(self):
        self.storage = InMemoryStateStorage()

    def test_save_and_load(self):
        self.storage.save("node1", {"key": "value"})
        result = self.storage.load("node1")
        assert result == {"key": "value"}

    def test_load_missing_returns_none(self):
        result = self.storage.load("nonexistent")
        assert result is None

    def test_delete_existing(self):
        self.storage.save("node1", {"key": "val"})
        self.storage.delete("node1")
        assert self.storage.load("node1") is None

    def test_delete_missing_no_error(self):
        self.storage.delete("nonexistent")  # should not raise

    def test_keys_empty(self):
        assert self.storage.keys() == []

    def test_keys_after_saves(self):
        self.storage.save("a", {})
        self.storage.save("b", {})
        keys = self.storage.keys()
        assert "a" in keys
        assert "b" in keys
        assert len(keys) == 2

    def test_keys_after_delete(self):
        self.storage.save("a", {})
        self.storage.save("b", {})
        self.storage.delete("a")
        keys = self.storage.keys()
        assert "a" not in keys
        assert "b" in keys

    def test_clear(self):
        self.storage.save("a", {"x": 1})
        self.storage.save("b", {"y": 2})
        self.storage.clear()
        assert self.storage.keys() == []
        assert self.storage.load("a") is None

    def test_overwrite(self):
        self.storage.save("node1", {"v": 1})
        self.storage.save("node1", {"v": 2})
        assert self.storage.load("node1") == {"v": 2}

    def test_save_complex_state(self):
        state = {
            "messages": [{"role": "user", "content": "hi"}],
            "tokens": 42,
            "nested": {"a": {"b": "c"}},
        }
        self.storage.save("complex", state)
        result = self.storage.load("complex")
        assert result == state

    def test_multiple_nodes_independent(self):
        self.storage.save("n1", {"data": "first"})
        self.storage.save("n2", {"data": "second"})
        assert self.storage.load("n1") == {"data": "first"}
        assert self.storage.load("n2") == {"data": "second"}


# ─────────────────────────── FileStateStorage ─────────────────────────────────


class TestFileStateStorage:
    @pytest.fixture
    def storage(self, tmp_path):
        return FileStateStorage(tmp_path / "states")

    def test_save_and_load(self, storage):
        storage.save("node1", {"key": "value"})
        result = storage.load("node1")
        assert result == {"key": "value"}

    def test_load_missing_returns_none(self, storage):
        result = storage.load("nonexistent")
        assert result is None

    def test_delete_existing(self, storage):
        storage.save("node1", {"key": "val"})
        storage.delete("node1")
        assert storage.load("node1") is None

    def test_delete_missing_no_error(self, storage):
        storage.delete("nonexistent")  # should not raise

    def test_keys_empty(self, storage):
        assert storage.keys() == []

    def test_keys_after_saves(self, storage):
        storage.save("a", {})
        storage.save("b", {})
        keys = storage.keys()
        assert "a" in keys
        assert "b" in keys

    def test_keys_after_delete(self, storage):
        storage.save("a", {})
        storage.save("b", {})
        storage.delete("a")
        keys = storage.keys()
        assert "a" not in keys

    def test_clear(self, storage):
        storage.save("a", {"x": 1})
        storage.save("b", {"y": 2})
        storage.clear()
        assert storage.keys() == []
        assert storage.load("a") is None

    def test_overwrite(self, storage):
        storage.save("node1", {"v": 1})
        storage.save("node1", {"v": 2})
        assert storage.load("node1") == {"v": 2}

    def test_safe_node_id_chars(self, storage):
        """Node IDs with special chars should be sanitized to safe filenames."""
        storage.save("agent/with:special", {"data": 42})
        result = storage.load("agent/with:special")
        assert result == {"data": 42}

    def test_complex_state(self, storage):
        state = {
            "messages": [{"role": "user", "content": "hello"}],
            "nested": {"key": [1, 2, 3]},
        }
        storage.save("node1", state)
        result = storage.load("node1")
        assert result == state

    def test_creates_directory(self, tmp_path):
        deep_path = tmp_path / "a" / "b" / "c"
        storage = FileStateStorage(deep_path)
        storage.save("test", {"v": 1})
        assert storage.load("test") == {"v": 1}

    def test_unicode_state(self, storage):
        state = {"message": "привет мир", "emoji": "🚀"}
        storage.save("unicode_node", state)
        result = storage.load("unicode_node")
        assert result == state

    def test_large_state(self, storage):
        state = {"data": list(range(1000))}
        storage.save("large_node", state)
        result = storage.load("large_node")
        assert result == state

    def test_keys_with_invalid_json_file(self, tmp_path):
        """keys() skips files with invalid JSON (lines 67-68)."""
        import json

        storage = FileStateStorage(str(tmp_path))

        # Write a valid file
        valid_file = tmp_path / "valid.json"
        valid_file.write_text(json.dumps({"node_id": "valid_node"}))

        # Write a file with invalid JSON
        invalid_file = tmp_path / "invalid.json"
        invalid_file.write_text("not valid json {")

        # Write a file missing node_id
        missing_id_file = tmp_path / "missing.json"
        missing_id_file.write_text(json.dumps({"other_key": "value"}))

        keys = storage.keys()
        assert "valid_node" in keys
        assert len([k for k in keys if k == "valid_node"]) == 1
