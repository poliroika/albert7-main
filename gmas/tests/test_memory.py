import time
from typing import Any

import pytest
import torch

from gmas.utils.memory import (
    AgentMemory,
    HiddenChannel,
    MemoryConfig,
    MemoryEntry,
    MemoryLevel,
    Message,
    MessageProtocol,
    RoleFamilyFilter,
    SharedMemoryPool,
    SharingPolicy,
    SubgraphFilter,
    SummaryCompressor,
    TagBasedFilter,
    TruncateCompressor,
)


class TestMemoryEntry:
    """Tests for MemoryEntry."""

    def test_create_default(self):
        """Create an entry with default parameters."""
        entry = MemoryEntry(content={"text": "hello"})

        assert entry.content == {"text": "hello"}
        assert entry.level == MemoryLevel.WORKING
        assert entry.priority == 0
        assert entry.ttl is None
        assert entry.source_agent is None

    def test_create_with_all_fields(self):
        """Create an entry specifying all fields."""
        entry = MemoryEntry(
            content={"role": "user", "content": "hi"},
            level=MemoryLevel.LONG_TERM,
            ttl=3600.0,
            priority=5,
            tags={"shared", "very imp"},
            source_agent="agent_a",
        )

        assert entry.level == MemoryLevel.LONG_TERM
        assert entry.ttl == 3600.0
        assert entry.priority == 5
        assert entry.tags == {"shared", "very imp"}
        assert entry.source_agent == "agent_a"

    def test_not_expired_without_ttl(self):
        """Entry without TTL never expires."""
        entry = MemoryEntry(content={"text": "hi"}, ttl=None)
        assert not entry.is_expired

    def test_not_expired_with_long_ttl(self):
        """Entry with a long TTL does not expire immediately."""
        entry = MemoryEntry(content={"text": "hi"}, ttl=3600.0)
        assert not entry.is_expired

    def test_expired_with_short_ttl(self):
        """Entry with a very short TTL expires after the delay."""
        entry = MemoryEntry(content={"text": "hi"}, ttl=0.01)
        time.sleep(0.02)
        assert entry.is_expired

    def test_touch_updates_accessed_at(self):
        """touch() updates the last-accessed timestamp."""
        entry = MemoryEntry(content={"text": "hi"})
        old_time = entry.accessed_at
        time.sleep(0.01)
        entry.touch()
        assert entry.accessed_at > old_time

    def test_age_property(self):
        """Age reflects time elapsed since creation."""
        entry = MemoryEntry(content={"text": "hi"})
        time.sleep(0.01)
        assert entry.age >= 0.01
        assert entry.age < 1.0


class TestTruncateCompressor:
    """Tests for TruncateCompressor."""

    def _make_entry(self, priority: int = 0, accessed_at: float = 0.0) -> MemoryEntry:
        entry = MemoryEntry(content={"text": "data"}, priority=priority)
        entry.accessed_at = accessed_at
        return entry

    def test_no_compression_needed(self):
        """No compression when entry count is within limit."""
        comp = TruncateCompressor()
        entries = [MemoryEntry(content={"i": i}) for i in range(3)]

        result = comp.compress(entries, max_entries=5)

        assert result is entries
        assert len(result) == 3

    def test_exact_limit(self):
        """Entry count exactly at max_entries — returned unchanged."""
        comp = TruncateCompressor()
        entries = [MemoryEntry(content={"i": i}) for i in range(5)]

        result = comp.compress(entries, max_entries=5)

        assert result is entries
        assert len(result) == 5

    def test_empty_list(self):
        """Empty input list returns empty list."""
        comp = TruncateCompressor()
        result = comp.compress([], max_entries=3)
        assert result == []

    def test_keeps_high_priority_and_recent(self):
        """By default, high-priority and recent entries are preserved."""
        comp = TruncateCompressor()

        e_low_old = self._make_entry(priority=0, accessed_at=100.0)
        e_low_new = self._make_entry(priority=0, accessed_at=300.0)
        e_high_old = self._make_entry(priority=10, accessed_at=100.0)
        e_high_new = self._make_entry(priority=10, accessed_at=300.0)

        entries = [e_low_old, e_low_new, e_high_old, e_high_new]

        result = comp.compress(entries, max_entries=2)

        assert len(result) == 2
        assert e_high_new in result
        assert e_high_old in result

    def test_priority_vs_recency(self):
        """High priority beats high recency when both can't fit."""
        comp = TruncateCompressor()

        e_low_new = self._make_entry(priority=0, accessed_at=999.0)
        e_high_old = self._make_entry(priority=10, accessed_at=1.0)

        result = comp.compress([e_low_new, e_high_old], max_entries=1)

        assert len(result) == 1
        assert result[0] is e_high_old

    def test_keep_recent_false(self):
        """keep_recent=False — older entries survive."""
        comp = TruncateCompressor(keep_recent=False, keep_high_priority=False)

        e_old = self._make_entry(priority=0, accessed_at=100.0)
        e_new = self._make_entry(priority=0, accessed_at=999.0)

        result = comp.compress([e_new, e_old], max_entries=1)

        assert len(result) == 1
        assert result[0] is e_old

    def test_keep_high_priority_false(self):
        """keep_high_priority=False — priority is ignored, only recency matters."""
        comp = TruncateCompressor(keep_recent=True, keep_high_priority=False)

        e_high_old = self._make_entry(priority=99, accessed_at=1.0)
        e_low_new = self._make_entry(priority=0, accessed_at=999.0)

        result = comp.compress([e_high_old, e_low_new], max_entries=1)

        assert len(result) == 1
        assert result[0] is e_low_new


class TestSummaryCompressor:
    """Tests for SummaryCompressor."""

    @staticmethod
    def _fake_summarizer(contents: list[dict[str, Any]]) -> dict[str, Any]:
        """Joins texts with ' | '."""
        texts = [c.get("text", "") for c in contents]
        return {"text": "Summary: " + " | ".join(texts)}

    def test_no_compression_within_limit(self):
        """No compression when entry count is within limit."""
        comp = SummaryCompressor(summarizer=self._fake_summarizer)
        entries = [MemoryEntry(content={"text": f"msg{i}"}) for i in range(3)]

        result = comp.compress(entries, max_entries=5)
        assert result is entries
        assert len(result) == 3

    def test_no_summarizer_truncates(self):
        """Without a summarizer, entries are simply truncated."""
        comp = SummaryCompressor(summarizer=None)
        entries = [MemoryEntry(content={"text": f"msg{i}"}) for i in range(5)]

        result = comp.compress(entries, max_entries=3)

        assert len(result) == 3
        assert result[0].content == {"text": "msg2"}
        assert result[1].content == {"text": "msg3"}
        assert result[2].content == {"text": "msg4"}

    def test_summarizes_old_entries(self):
        """Old entries are summarized; recent entries are kept as-is."""
        comp = SummaryCompressor(summarizer=self._fake_summarizer)
        entries = [MemoryEntry(content={"text": f"msg{i}"}) for i in range(5)]

        result = comp.compress(entries, max_entries=3)

        assert len(result) == 3
        assert "Summary:" in result[0].content["text"]
        assert "msg0" in result[0].content["text"]
        assert "msg1" in result[0].content["text"]
        assert "msg2" in result[0].content["text"]
        assert result[1].content == {"text": "msg3"}
        assert result[2].content == {"text": "msg4"}

    def test_summary_entry_level_is_long_term(self):
        """Summary entry always has LONG_TERM level."""
        comp = SummaryCompressor(summarizer=self._fake_summarizer)
        entries = [MemoryEntry(content={"text": f"msg{i}"}) for i in range(5)]

        result = comp.compress(entries, max_entries=3)

        assert result[0].level == MemoryLevel.LONG_TERM

    def test_summary_entry_max_priority(self):
        """Summary entry receives the maximum priority from compressed entries."""
        comp = SummaryCompressor(summarizer=self._fake_summarizer)
        entries = [
            MemoryEntry(content={"text": "a"}, priority=2),
            MemoryEntry(content={"text": "b"}, priority=7),
            MemoryEntry(content={"text": "c"}, priority=3),
            MemoryEntry(content={"text": "d"}, priority=1),
        ]

        result = comp.compress(entries, max_entries=2)

        assert result[0].priority == 7

    def test_summary_entry_tags_merged(self):
        """Summary entry merges tags from all compressed entries."""
        comp = SummaryCompressor(summarizer=self._fake_summarizer)
        entries = [
            MemoryEntry(content={"text": "a"}, tags={"alpha", "beta"}),
            MemoryEntry(content={"text": "b"}, tags={"beta", "gamma"}),
            MemoryEntry(content={"text": "c"}, tags={"delta"}),
            MemoryEntry(content={"text": "d"}, tags={"epsilon"}),
        ]

        result = comp.compress(entries, max_entries=2)

        assert result[0].tags == {"alpha", "beta", "gamma", "delta"}


class TestMemoryConfig:
    """Tests for MemoryConfig."""

    def test_default_values(self):
        """All default values are correct."""
        cfg = MemoryConfig()

        assert cfg.working_max_entries == 20
        assert cfg.working_default_ttl == 3600.0
        assert cfg.long_term_max_entries == 100
        assert cfg.long_term_default_ttl is None
        assert cfg.auto_compress is True
        assert cfg.promote_after_accesses == 3
        assert cfg.demote_inactive_after == 7200.0
        assert cfg.cleanup_interval == 300.0
        assert cfg.hidden_state_dim is None

    def test_default_compression_strategy(self):
        """Default compression strategy is TruncateCompressor."""
        cfg = MemoryConfig()

        assert isinstance(cfg.compression_strategy, TruncateCompressor)

    def test_custom_values(self):
        """All parameters can be overridden."""
        custom_comp = SummaryCompressor()
        cfg = MemoryConfig(
            working_max_entries=50,
            working_default_ttl=None,
            long_term_max_entries=200,
            long_term_default_ttl=86400.0,
            compression_strategy=custom_comp,
            auto_compress=False,
            promote_after_accesses=10,
            demote_inactive_after=3600.0,
            cleanup_interval=60.0,
            hidden_state_dim=128,
        )

        assert cfg.working_max_entries == 50
        assert cfg.working_default_ttl is None
        assert cfg.long_term_max_entries == 200
        assert cfg.long_term_default_ttl == 86400.0
        assert cfg.compression_strategy is custom_comp
        assert cfg.auto_compress is False
        assert cfg.promote_after_accesses == 10
        assert cfg.demote_inactive_after == 3600.0
        assert cfg.cleanup_interval == 60.0
        assert cfg.hidden_state_dim == 128


class TestAgentMemory:
    """Tests for AgentMemory."""

    def test_init_default_config(self):
        """Create with default config."""
        mem = AgentMemory("agent_1")
        assert mem.agent_id == "agent_1"
        assert isinstance(mem.config, MemoryConfig)
        assert mem._working == []
        assert mem._long_term == []

    def test_init_custom_config(self):
        """Create with custom config."""
        cfg = MemoryConfig(working_max_entries=5)
        mem = AgentMemory("agent_2", config=cfg)
        assert mem.config.working_max_entries == 5

    def test_add_to_working(self):
        """add() places entry in working memory by default."""
        mem = AgentMemory("a")
        entry = mem.add(content={"text": "hi"})
        assert entry in mem._working
        assert entry not in mem._long_term
        assert entry.level == MemoryLevel.WORKING

    def test_add_to_long_term(self):
        """add() with level=LONG_TERM places entry in long-term memory."""
        mem = AgentMemory("a")
        entry = mem.add(content={"text": "hi"}, level=MemoryLevel.LONG_TERM)

        assert entry in mem._long_term
        assert entry not in mem._working
        assert entry.level == MemoryLevel.LONG_TERM

    def test_add_default_ttl_working(self):
        """Default TTL from config is applied to working entries."""
        cfg = MemoryConfig(working_default_ttl=999.0)
        mem = AgentMemory("a", config=cfg)
        entry = mem.add(content={"text": "hi"})

        assert entry.ttl == 999.0

    def test_add_default_ttl_long_term(self):
        """Default TTL from config is applied to long-term entries."""
        cfg = MemoryConfig(long_term_default_ttl=5000.0)
        mem = AgentMemory("a", config=cfg)
        entry = mem.add(content={"text": "hi"}, level=MemoryLevel.LONG_TERM)

        assert entry.ttl == 5000.0

    def test_add_explicit_ttl(self):
        """Explicit TTL overrides the config default."""
        mem = AgentMemory("a")
        entry = mem.add(content={"text": "hi"}, ttl=42.0)

        assert entry.ttl == 42.0

    def test_add_with_tags_and_priority(self):
        """Tags and priority are stored in the entry."""
        mem = AgentMemory("a")
        entry = mem.add(
            content={"text": "hi"},
            priority=5,
            tags={"urgent"},
            source_agent="agent_b",
        )

        assert entry.priority == 5
        assert entry.tags == {"urgent"}
        assert entry.source_agent == "agent_b"

    def test_add_message(self):
        """add_message() creates a working entry with role/content."""
        mem = AgentMemory("a")
        entry = mem.add_message(role="user", content="hello")

        assert entry.content["role"] == "user"
        assert entry.content["content"] == "hello"
        assert entry.level == MemoryLevel.WORKING

    def test_add_message_with_metadata(self):
        """add_message() passes additional metadata into the entry."""
        mem = AgentMemory("a")
        entry = mem.add_message(role="assistant", content="reply", tool_call_id="123")

        assert entry.content["tool_call_id"] == "123"

    def test_get_all(self):
        """get() without filters returns all entries."""
        mem = AgentMemory("a")
        mem.add(content={"i": 1})
        mem.add(content={"i": 2}, level=MemoryLevel.LONG_TERM)

        result = mem.get()
        assert len(result) == 2

    def test_get_by_level(self):
        """get() with level filter returns only entries at that level."""
        mem = AgentMemory("a")
        mem.add(content={"i": 1})
        mem.add(content={"i": 2}, level=MemoryLevel.LONG_TERM)

        working = mem.get(level=MemoryLevel.WORKING)
        assert len(working) == 1
        assert working[0].content == {"i": 1}

        lt = mem.get(level=MemoryLevel.LONG_TERM)
        assert len(lt) == 1
        assert lt[0].content == {"i": 2}

    def test_get_filters_expired(self):
        """get() excludes expired entries by default."""
        mem = AgentMemory("a")
        mem.add(content={"text": "old"}, ttl=0.01)
        mem.add(content={"text": "fresh"}, ttl=9999.0)
        time.sleep(0.02)

        result = mem.get()
        assert len(result) == 1
        assert result[0].content["text"] == "fresh"

    def test_get_include_expired(self):
        """get(include_expired=True) returns expired entries as well."""
        mem = AgentMemory("a")
        mem.add(content={"text": "old"}, ttl=0.01)
        time.sleep(0.02)

        result = mem.get(include_expired=True)
        assert len(result) == 1

    def test_get_by_tags(self):
        """get(tags=...) returns only entries with matching tags."""
        mem = AgentMemory("a")
        mem.add(content={"i": 1}, tags={"alpha"})
        mem.add(content={"i": 2}, tags={"beta"})
        mem.add(content={"i": 3}, tags={"alpha", "beta"})

        result = mem.get(tags={"alpha"})
        assert len(result) == 2

    def test_get_with_limit(self):
        """get(limit=N) returns the last N entries."""
        mem = AgentMemory("a")
        for i in range(10):
            mem.add(content={"i": i})

        result = mem.get(limit=3)
        assert len(result) == 3
        assert result[0].content == {"i": 7}
        assert result[2].content == {"i": 9}

    def test_promote_after_accesses(self):
        """Entry is promoted to long-term after N accesses via get()."""
        cfg = MemoryConfig(promote_after_accesses=3)
        mem = AgentMemory("a", config=cfg)
        mem.add(content={"text": "test"})

        mem.get()
        mem.get()
        assert len(mem._working) == 1

        mem.get()
        assert len(mem._working) == 0
        assert len(mem._long_term) == 1
        assert mem._long_term[0].level == MemoryLevel.LONG_TERM

    def test_get_messages(self):
        """get_messages() returns only entries that have a 'role' field."""
        mem = AgentMemory("a")
        mem.add_message(role="user", content="hi")
        mem.add(content={"text": "not a message"})

        messages = mem.get_messages()
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    def test_get_messages_with_limit(self):
        """get_messages(limit=N) limits the number of returned messages."""
        mem = AgentMemory("a")
        for i in range(5):
            mem.add_message(role="user", content=f"msg{i}")

        messages = mem.get_messages(limit=2)
        assert len(messages) == 2

    def test_clear_all(self):
        """clear() without arguments clears all memory levels."""
        mem = AgentMemory("a")
        mem.add(content={"i": 1})
        mem.add(content={"i": 2}, level=MemoryLevel.LONG_TERM)

        mem.clear()

        assert mem._working == []
        assert mem._long_term == []

    def test_clear_working_only(self):
        """clear(WORKING) clears only working memory."""
        mem = AgentMemory("a")
        mem.add(content={"i": 1})
        mem.add(content={"i": 2}, level=MemoryLevel.LONG_TERM)

        mem.clear(level=MemoryLevel.WORKING)

        assert mem._working == []
        assert len(mem._long_term) == 1

    def test_clear_long_term_only(self):
        """clear(LONG_TERM) clears only long-term memory."""
        mem = AgentMemory("a")
        mem.add(content={"i": 1})
        mem.add(content={"i": 2}, level=MemoryLevel.LONG_TERM)

        mem.clear(level=MemoryLevel.LONG_TERM)

        assert len(mem._working) == 1
        assert mem._long_term == []

    def test_remove_expired(self):
        """remove_expired() deletes expired entries and returns count removed."""
        mem = AgentMemory("a")
        mem.add(content={"text": "old"}, ttl=0.01)
        mem.add(content={"text": "fresh"}, ttl=9999.0)
        time.sleep(0.02)

        removed = mem.remove_expired()

        assert removed == 1
        assert len(mem._working) == 1
        assert mem._working[0].content["text"] == "fresh"

    def test_auto_compress_working(self):
        """Overflow in working memory triggers automatic compression."""
        cfg = MemoryConfig(working_max_entries=3, auto_compress=True)
        mem = AgentMemory("a", config=cfg)

        for i in range(5):
            mem.add(content={"i": i})

        assert len(mem._working) <= 3

    def test_auto_compress_disabled(self):
        """auto_compress=False — entries are not compressed."""
        cfg = MemoryConfig(working_max_entries=3, auto_compress=False)
        mem = AgentMemory("a", config=cfg)

        for i in range(5):
            mem.add(content={"i": i})

        assert len(mem._working) == 5

    def test_working_memory_property(self):
        """working_memory returns only non-expired entries from working level."""
        mem = AgentMemory("a")
        mem.add(content={"text": "alive"}, ttl=9999.0)
        mem.add(content={"text": "dead"}, ttl=0.01)
        time.sleep(0.02)

        result = mem.working_memory
        assert len(result) == 1
        assert result[0].content["text"] == "alive"

    def test_all_entries_property(self):
        """all_entries combines working and long-term, excluding expired."""
        mem = AgentMemory("a")
        mem.add(content={"i": 1})
        mem.add(content={"i": 2}, level=MemoryLevel.LONG_TERM)

        assert len(mem.all_entries) == 2

    def test_hidden_state_and_embedding(self):
        """hidden_state and embedding are stored and retrieved correctly."""
        mem = AgentMemory("a")

        assert mem.hidden_state is None
        assert mem.embedding is None

        hs = torch.randn(64)
        emb = torch.randn(128)
        mem.hidden_state = hs
        mem.embedding = emb

        assert mem.hidden_state is not None
        assert torch.equal(mem.hidden_state, hs)
        assert mem.embedding is not None
        assert torch.equal(mem.embedding, emb)


class TestAgentMemorySerialization:
    """Tests for to_dict / from_dict."""

    def test_to_dict_empty(self):
        """Empty memory serializes correctly."""
        mem = AgentMemory("agent_1")
        d = mem.to_dict()

        assert d["agent_id"] == "agent_1"
        assert d["working"] == []
        assert d["long_term"] == []
        assert d["hidden_state"] is None
        assert d["embedding"] is None

    def test_to_dict_with_long_term_entry(self):
        """Long-term entries appear under their own key."""
        mem = AgentMemory("a")
        mem.add(content={"text": "important"}, level=MemoryLevel.LONG_TERM, priority=9)
        d = mem.to_dict()
        assert len(d["working"]) == 0
        assert len(d["long_term"]) == 1
        assert d["long_term"][0]["content"] == {"text": "important"}
        assert d["long_term"][0]["priority"] == 9

    def test_to_dict_with_hidden_state_and_embedding(self):
        """hidden_state and embedding are serialized as lists."""
        mem = AgentMemory("a")
        mem.hidden_state = torch.tensor([1.0, 2.0, 3.0])
        mem.embedding = torch.tensor([4.0, 5.0])
        d = mem.to_dict()
        assert d["hidden_state"] == [1.0, 2.0, 3.0]
        assert d["embedding"] == [4.0, 5.0]

    def test_to_dict_tags_serialized_as_list(self):
        """Tags (set) are converted to list during serialization."""
        mem = AgentMemory("a")
        mem.add(content={"x": 1}, tags={"a", "b", "c"})
        d = mem.to_dict()
        assert isinstance(d["working"][0]["tags"], list)
        assert set(d["working"][0]["tags"]) == {"a", "b", "c"}

    def test_to_dict_does_not_include_source_agent(self):  # TODO: may need to revisit this behavior
        """source_agent is NOT saved during serialization (data loss)."""
        mem = AgentMemory("a")
        mem.add(content={"x": 1}, source_agent="agent_b")
        d = mem.to_dict()
        assert "source_agent" not in d["working"][0]

    def test_to_dict_does_not_include_accessed_at(self):  # TODO: may need to revisit this behavior
        """accessed_at is NOT saved during serialization."""
        mem = AgentMemory("a")
        mem.add(content={"x": 1})
        d = mem.to_dict()
        assert "accessed_at" not in d["working"][0]

    def test_from_dict_basic(self):
        """from_dict() restores agent_id and all entry fields."""
        data = {
            "agent_id": "agent_x",
            "working": [
                {"content": {"text": "hi"}, "priority": 2, "tags": ["a"], "ttl": 100.0, "created_at": 1000.0},
            ],
            "long_term": [
                {"content": {"text": "old"}, "priority": 5, "tags": ["b", "c"], "ttl": None, "created_at": 900.0},
            ],
            "hidden_state": None,
            "embedding": None,
        }
        mem = AgentMemory.from_dict(data)
        assert mem.agent_id == "agent_x"
        assert len(mem._working) == 1
        assert len(mem._long_term) == 1
        assert mem._working[0].content == {"text": "hi"}
        assert mem._working[0].priority == 2
        assert mem._working[0].tags == {"a"}
        assert mem._working[0].ttl == 100.0
        assert mem._working[0].created_at == 1000.0
        assert mem._working[0].level == MemoryLevel.WORKING
        assert mem._long_term[0].content == {"text": "old"}
        assert mem._long_term[0].level == MemoryLevel.LONG_TERM
        assert mem._long_term[0].tags == {"b", "c"}

    def test_from_dict_with_custom_config(self):
        """from_dict() accepts a custom config."""
        data = {"agent_id": "a", "working": [], "long_term": []}
        cfg = MemoryConfig(working_max_entries=7)
        mem = AgentMemory.from_dict(data, config=cfg)
        assert mem.config.working_max_entries == 7

    def test_from_dict_default_config(self):
        """from_dict() uses default MemoryConfig when none is provided."""
        data = {"agent_id": "a", "working": [], "long_term": []}
        mem = AgentMemory.from_dict(data)
        assert isinstance(mem.config, MemoryConfig)
        assert mem.config.working_max_entries == 20

    def test_from_dict_restores_hidden_state(self):
        """from_dict() restores hidden_state and embedding from lists."""
        data = {
            "agent_id": "a",
            "working": [],
            "long_term": [],
            "hidden_state": [1.0, 2.0, 3.0],
            "embedding": [4.0, 5.0],
        }
        mem = AgentMemory.from_dict(data)
        assert mem.hidden_state is not None
        assert torch.equal(mem.hidden_state, torch.tensor([1.0, 2.0, 3.0]))
        assert mem.embedding is not None
        assert torch.equal(mem.embedding, torch.tensor([4.0, 5.0]))

    def test_from_dict_missing_optional_fields(self):
        """from_dict() with minimal data uses sensible defaults."""
        data = {
            "agent_id": "a",
            "working": [{"content": {"msg": "hi"}}],
            "long_term": [],
        }
        mem = AgentMemory.from_dict(data)
        entry = mem._working[0]
        assert entry.content == {"msg": "hi"}
        assert entry.priority == 0
        assert entry.tags == set()
        assert entry.ttl is None
        assert entry.created_at > 0

    def test_roundtrip(self):
        """to_dict → from_dict preserves all data."""
        mem = AgentMemory("rt_agent")
        mem.add(content={"text": "w1"}, priority=1, tags={"x"}, ttl=500.0)
        mem.add(content={"text": "w2"}, priority=2, tags={"y", "z"})
        mem.add(content={"text": "lt1"}, level=MemoryLevel.LONG_TERM, priority=8)
        mem.hidden_state = torch.tensor([1.0, 2.0])
        mem.embedding = torch.tensor([3.0, 4.0, 5.0])

        d = mem.to_dict()
        restored = AgentMemory.from_dict(d)

        assert restored.agent_id == "rt_agent"
        assert len(restored._working) == 2
        assert len(restored._long_term) == 1
        assert restored._working[0].content == {"text": "w1"}
        assert restored._working[0].priority == 1
        assert restored._working[0].tags == {"x"}
        assert restored._working[0].ttl == 500.0
        assert restored._working[1].content == {"text": "w2"}
        assert restored._working[1].tags == {"y", "z"}
        assert restored._long_term[0].content == {"text": "lt1"}
        assert restored._long_term[0].priority == 8
        assert restored.hidden_state is not None
        assert torch.equal(restored.hidden_state, torch.tensor([1.0, 2.0]))
        assert restored.embedding is not None
        assert torch.equal(restored.embedding, torch.tensor([3.0, 4.0, 5.0]))

    def test_roundtrip_preserves_created_at(self):
        """Round-trip preserves created_at timestamp."""
        mem = AgentMemory("a")
        entry = mem.add(content={"x": 1})
        original_created = entry.created_at

        restored = AgentMemory.from_dict(mem.to_dict())
        assert restored._working[0].created_at == pytest.approx(original_created, abs=0.001)

    def test_roundtrip_loses_source_agent(self):  # TODO: may need to revisit this behavior
        """Round-trip does not preserve source_agent."""
        mem = AgentMemory("a")
        mem.add(content={"x": 1}, source_agent="agent_b")

        restored = AgentMemory.from_dict(mem.to_dict())
        assert restored._working[0].source_agent is None


class TestAccessFilters:
    """Tests for TagBasedFilter, SubgraphFilter, RoleFamilyFilter."""

    def test_tag_filter_allows_on_intersection(self):
        """Access is granted when entry tags intersect with shared_tags."""
        f = TagBasedFilter(shared_tags={"shared", "public"})
        entry = MemoryEntry(content={"x": 1}, tags={"shared", "internal"})
        assert f.can_access("agent_a", "agent_b", entry) is True

    def test_tag_filter_denies_no_intersection(self):
        """Access is denied when there is no tag intersection."""
        f = TagBasedFilter(shared_tags={"shared", "public"})
        entry = MemoryEntry(content={"x": 1}, tags={"private", "internal"})
        assert f.can_access("agent_a", "agent_b", entry) is False

    def test_tag_filter_empty_entry_tags(self):
        """Access is denied when the entry has no tags."""
        f = TagBasedFilter(shared_tags={"shared"})
        entry = MemoryEntry(content={"x": 1}, tags=set())
        assert f.can_access("agent_a", "agent_b", entry) is False

    def test_tag_filter_empty_shared_tags(self):
        """Access is denied when shared_tags is empty."""
        f = TagBasedFilter(shared_tags=set())
        entry = MemoryEntry(content={"x": 1}, tags={"shared"})
        assert f.can_access("agent_a", "agent_b", entry) is False

    def test_tag_filter_ignores_agent_ids(self):
        """TagBasedFilter does not depend on requester or owner IDs."""
        f = TagBasedFilter(shared_tags={"ok"})
        entry = MemoryEntry(content={"x": 1}, tags={"ok"})
        assert f.can_access("any", "who", entry) is True

    def test_subgraph_filter_same_subgraph_allows(self):
        """Agents in the same subgraph are allowed access."""
        f = SubgraphFilter(
            subgraph_members={
                "sg1": {"agent_a", "agent_b"},
                "sg2": {"agent_c"},
            }
        )
        entry = MemoryEntry(content={"x": 1})
        assert f.can_access("agent_a", "agent_b", entry) is True

    def test_subgraph_filter_different_subgraph_denies(self):
        """Agents in different subgraphs are denied access."""
        f = SubgraphFilter(
            subgraph_members={
                "sg1": {"agent_a"},
                "sg2": {"agent_b"},
            }
        )
        entry = MemoryEntry(content={"x": 1})
        assert f.can_access("agent_a", "agent_b", entry) is False

    def test_subgraph_filter_unknown_requester_denies(self):
        """Unknown requester is denied access."""
        f = SubgraphFilter(subgraph_members={"sg1": {"agent_a"}})
        entry = MemoryEntry(content={"x": 1})
        assert f.can_access("unknown", "agent_a", entry) is False

    def test_subgraph_filter_unknown_owner_denies(self):
        """Unknown owner is denied access."""
        f = SubgraphFilter(subgraph_members={"sg1": {"agent_a"}})
        entry = MemoryEntry(content={"x": 1})
        assert f.can_access("agent_a", "unknown", entry) is False

    def test_subgraph_filter_ignores_entry(self):
        """SubgraphFilter does not depend on the entry's content."""
        f = SubgraphFilter(subgraph_members={"sg1": {"a", "b"}})
        entry = MemoryEntry(content={"x": 1}, tags={"secret"})
        assert f.can_access("a", "b", entry) is True

    def test_subgraph_agent_to_subgraph_mapping(self):
        """Reverse index _agent_to_subgraph is built correctly."""
        f = SubgraphFilter(
            subgraph_members={
                "analysis": {"solver", "reviewer"},
                "output": {"writer"},
            }
        )
        assert f._agent_to_subgraph["solver"] == "analysis"
        assert f._agent_to_subgraph["reviewer"] == "analysis"
        assert f._agent_to_subgraph["writer"] == "output"

    def test_role_family_same_family_allows(self):
        """Agents from the same role family are allowed access."""
        f = RoleFamilyFilter(
            role_families={
                "analysts": {"data_analyst", "market_analyst"},
                "writers": {"copywriter", "editor"},
            }
        )
        entry = MemoryEntry(content={"x": 1})
        assert f.can_access("data_analyst", "market_analyst", entry) is True

    def test_role_family_different_family_denies(self):
        """Agents from different role families are denied access."""
        f = RoleFamilyFilter(
            role_families={
                "analysts": {"data_analyst"},
                "writers": {"copywriter"},
            }
        )
        entry = MemoryEntry(content={"x": 1})
        assert f.can_access("data_analyst", "copywriter", entry) is False

    def test_role_family_unknown_requester_denies(self):
        """Requester not belonging to any family is denied access."""
        f = RoleFamilyFilter(role_families={"analysts": {"data_analyst"}})
        entry = MemoryEntry(content={"x": 1})
        assert f.can_access("outsider", "data_analyst", entry) is False

    def test_role_family_unknown_owner_denies(self):
        """Owner not belonging to any family is denied access."""
        f = RoleFamilyFilter(role_families={"analysts": {"data_analyst"}})
        entry = MemoryEntry(content={"x": 1})
        assert f.can_access("data_analyst", "outsider", entry) is False

    def test_role_family_agent_to_family_mapping(self):
        """Reverse index _agent_to_family is built correctly."""
        f = RoleFamilyFilter(
            role_families={
                "analysts": {"solver", "reviewer"},
                "writers": {"editor"},
            }
        )
        assert f._agent_to_family["solver"] == "analysts"
        assert f._agent_to_family["reviewer"] == "analysts"
        assert f._agent_to_family["editor"] == "writers"

    def test_role_family_ignores_entry(self):
        """RoleFamilyFilter does not depend on the entry's content."""
        f = RoleFamilyFilter(role_families={"team": {"a", "b"}})
        entry = MemoryEntry(content={"x": 1}, tags={"secret"}, priority=99)
        assert f.can_access("a", "b", entry) is True


class TestSharedMemoryPool:
    """Tests for SharedMemoryPool."""

    def _make_pool_with_agents(self, *agent_ids: str) -> tuple[SharedMemoryPool, dict[str, AgentMemory]]:
        pool = SharedMemoryPool()
        memories = {}
        for aid in agent_ids:
            mem = AgentMemory(aid)
            pool.register(mem)
            memories[aid] = mem
        return pool, memories

    def test_init_defaults(self):
        """Pool is created with correct defaults."""
        pool = SharedMemoryPool()
        assert pool.access_filter is None
        assert pool.default_policy == SharingPolicy.BY_TAGS
        assert pool._memories == {}
        assert pool._shared_entries == []

    def test_register_and_unregister(self):
        """register/unregister adds and removes agent memory."""
        pool = SharedMemoryPool()
        mem = AgentMemory("a")
        pool.register(mem)
        assert "a" in pool._memories
        pool.unregister("a")
        assert "a" not in pool._memories

    def test_unregister_nonexistent(self):
        """Unregistering an unknown agent does not raise."""
        pool = SharedMemoryPool()
        pool.unregister("ghost")

    def test_share_to_specific_agents(self):
        """share() with to_agents places a copy in each recipient's working memory."""
        pool, mems = self._make_pool_with_agents("sender", "recv_1", "recv_2")
        entry = mems["sender"].add(content={"text": "hello"}, priority=3, tags={"info"})
        pool.share("sender", entry, to_agents=["recv_1", "recv_2"])

        r1_entries = mems["recv_1"].get()
        assert len(r1_entries) == 1
        assert r1_entries[0].content == {"text": "hello"}
        assert r1_entries[0].priority == 3
        assert "shared" in r1_entries[0].tags
        assert r1_entries[0].source_agent == "sender"
        assert r1_entries[0].level == MemoryLevel.WORKING

        r2_entries = mems["recv_2"].get()
        assert len(r2_entries) == 1

    def test_share_to_unknown_agent_ignored(self):
        """Sharing to a non-registered agent does not raise."""
        pool, mems = self._make_pool_with_agents("sender")
        entry = mems["sender"].add(content={"text": "hi"})
        pool.share("sender", entry, to_agents=["ghost"])

    def test_share_to_pool(self):
        """share() without to_agents places the entry in the shared pool."""
        pool, mems = self._make_pool_with_agents("sender")
        entry = mems["sender"].add(content={"text": "public"})
        pool.share("sender", entry)
        assert len(pool._shared_entries) == 1
        assert pool._shared_entries[0].source_agent == "sender"
        assert pool._shared_entries[0].level == MemoryLevel.SHARED

    def test_share_copies_content(self):
        """share() copies content rather than sharing the reference."""
        pool, mems = self._make_pool_with_agents("sender")
        original_content = {"text": "hello"}
        entry = mems["sender"].add(content=original_content)
        pool.share("sender", entry)
        pool._shared_entries[0].content["text"] = "modified"
        assert entry.content["text"] == "hello"

    def test_get_shared_no_filter(self):
        """get_shared() without a filter returns all non-expired entries."""
        pool, mems = self._make_pool_with_agents("a", "b")
        entry = mems["a"].add(content={"text": "data"})
        pool.share("a", entry)
        result = pool.get_shared("b")
        assert len(result) == 1
        assert result[0].content == {"text": "data"}

    def test_get_shared_filters_by_tags(self):
        """get_shared() with tags filters by tag intersection."""
        pool, mems = self._make_pool_with_agents("a")
        e1 = mems["a"].add(content={"i": 1}, tags={"alpha"})
        e2 = mems["a"].add(content={"i": 2}, tags={"beta"})
        pool.share("a", e1)
        pool.share("a", e2)
        result = pool.get_shared("anyone", tags={"alpha"})
        assert len(result) == 1
        assert result[0].content == {"i": 1}

    def test_get_shared_filters_expired(self):
        """get_shared() does not return expired entries."""
        pool, mems = self._make_pool_with_agents("a")
        entry = mems["a"].add(content={"text": "old"}, ttl=0.01)
        pool.share("a", entry)
        time.sleep(0.02)
        result = pool.get_shared("anyone")
        assert len(result) == 0

    def test_get_shared_with_limit(self):
        """get_shared() with limit returns the last N entries."""
        pool, mems = self._make_pool_with_agents("a")
        for i in range(5):
            entry = mems["a"].add(content={"i": i})
            pool.share("a", entry)
        result = pool.get_shared("anyone", limit=2)
        assert len(result) == 2
        assert result[0].content == {"i": 3}
        assert result[1].content == {"i": 4}

    def test_get_shared_with_access_filter(self):
        """get_shared() respects the access_filter."""
        f = SubgraphFilter(
            subgraph_members={
                "sg1": {"a", "b"},
                "sg2": {"c"},
            }
        )
        pool = SharedMemoryPool(access_filter=f)
        mem_a = AgentMemory("a")
        pool.register(mem_a)
        entry = mem_a.add(content={"text": "secret"})
        pool.share("a", entry)

        assert len(pool.get_shared("b")) == 1
        assert len(pool.get_shared("c")) == 0

    def test_get_from_agent_basic(self):
        """get_from_agent() returns entries from a specific agent."""
        pool, mems = self._make_pool_with_agents("a", "b")
        mems["a"].add(content={"text": "data_a"})
        result = pool.get_from_agent("b", "a")
        assert len(result) == 1
        assert result[0].content == {"text": "data_a"}

    def test_get_from_agent_unknown_owner(self):
        """get_from_agent() returns empty list for unknown owner."""
        pool = SharedMemoryPool()
        result = pool.get_from_agent("anyone", "ghost")
        assert result == []

    def test_get_from_agent_with_filter(self):
        """get_from_agent() respects the access_filter."""
        f = SubgraphFilter(
            subgraph_members={
                "sg1": {"a", "b"},
                "sg2": {"c"},
            }
        )
        pool = SharedMemoryPool(access_filter=f)
        mem_a = AgentMemory("a")
        pool.register(mem_a)
        mem_a.add(content={"text": "data"})

        assert len(pool.get_from_agent("b", "a")) == 1
        assert len(pool.get_from_agent("c", "a")) == 0

    def test_get_from_agent_with_level_filter(self):
        """get_from_agent() filters by level."""
        pool, mems = self._make_pool_with_agents("a", "b")
        mems["a"].add(content={"i": 1})
        mems["a"].add(content={"i": 2}, level=MemoryLevel.LONG_TERM)
        result = pool.get_from_agent("b", "a", level=MemoryLevel.WORKING)
        assert len(result) == 1
        assert result[0].content == {"i": 1}

    def test_get_from_agent_with_tags_filter(self):
        """get_from_agent() filters by tags."""
        pool, mems = self._make_pool_with_agents("a", "b")
        mems["a"].add(content={"i": 1}, tags={"alpha"})
        mems["a"].add(content={"i": 2}, tags={"beta"})
        result = pool.get_from_agent("b", "a", tags={"beta"})
        assert len(result) == 1
        assert result[0].content == {"i": 2}

    def test_broadcast(self):
        """broadcast() sends an entry to all agents except the sender."""
        pool, mems = self._make_pool_with_agents("sender", "recv_1", "recv_2")
        pool.broadcast("sender", content={"text": "hello all"}, tags={"news"})

        assert len(mems["sender"].get()) == 0
        r1 = mems["recv_1"].get()
        assert len(r1) == 1
        assert r1[0].content == {"text": "hello all"}
        assert "broadcast" in r1[0].tags
        assert "news" in r1[0].tags
        assert r1[0].source_agent == "sender"

        r2 = mems["recv_2"].get()
        assert len(r2) == 1

    def test_broadcast_no_tags(self):
        """broadcast() without tags adds only the 'broadcast' tag."""
        pool, mems = self._make_pool_with_agents("a", "b")
        pool.broadcast("a", content={"x": 1})
        entry = mems["b"].get()[0]
        assert entry.tags == {"broadcast"}


class TestHiddenChannel:
    """Tests for HiddenChannel."""

    def test_create_defaults(self):
        """Create with default values."""
        hc = HiddenChannel()
        assert hc.hidden_state is None
        assert hc.embedding is None
        assert hc.metadata == {}

    def test_create_with_tensors(self):
        """Create with tensor fields."""
        hs = torch.tensor([1.0, 2.0, 3.0])
        emb = torch.tensor([4.0, 5.0])
        hc = HiddenChannel(hidden_state=hs, embedding=emb, metadata={"key": "val"})
        assert hc.hidden_state is not None
        assert torch.equal(hc.hidden_state, hs)
        assert hc.embedding is not None
        assert torch.equal(hc.embedding, emb)
        assert hc.metadata == {"key": "val"}

    def test_to_dict_with_tensors(self):
        """to_dict converts tensors to lists."""
        hc = HiddenChannel(
            hidden_state=torch.tensor([1.0, 2.0]),
            embedding=torch.tensor([3.0]),
            metadata={"a": 1},
        )
        d = hc.to_dict()
        assert d["hidden_state"] == [1.0, 2.0]
        assert d["embedding"] == [3.0]
        assert d["metadata"] == {"a": 1}

    def test_to_dict_none_tensors(self):
        """to_dict with None tensors."""
        hc = HiddenChannel()
        d = hc.to_dict()
        assert d["hidden_state"] is None
        assert d["embedding"] is None
        assert d["metadata"] == {}

    def test_from_dict_with_tensors(self):
        """from_dict restores tensors from lists."""
        data = {
            "hidden_state": [1.0, 2.0],
            "embedding": [3.0, 4.0],
            "metadata": {"key": "val"},
        }
        hc = HiddenChannel.from_dict(data)
        assert hc.hidden_state is not None
        assert torch.equal(hc.hidden_state, torch.tensor([1.0, 2.0]))
        assert hc.embedding is not None
        assert torch.equal(hc.embedding, torch.tensor([3.0, 4.0]))
        assert hc.metadata == {"key": "val"}

    def test_from_dict_none_tensors(self):
        """from_dict with None tensors."""
        data = {"hidden_state": None, "embedding": None, "metadata": {}}
        hc = HiddenChannel.from_dict(data)
        assert hc.hidden_state is None
        assert hc.embedding is None

    def test_from_dict_missing_metadata(self):
        """from_dict without metadata defaults to empty dict."""
        data = {"hidden_state": None, "embedding": None}
        hc = HiddenChannel.from_dict(data)
        assert hc.metadata == {}


class TestMessage:
    """Tests for Message."""

    def test_create_minimal(self):
        """Create with minimal parameters."""
        msg = Message(sender_id="a", receiver_id=None, content="hello")
        assert msg.sender_id == "a"
        assert msg.receiver_id is None
        assert msg.content == "hello"
        assert msg.role == "assistant"
        assert msg.message_type == "response"
        assert msg.priority == 0
        assert msg.tags == set()
        assert msg.hidden is None
        assert msg.timestamp > 0

    def test_create_full(self):
        """Create with all fields."""
        hc = HiddenChannel(hidden_state=torch.tensor([1.0]))
        msg = Message(
            sender_id="a",
            receiver_id="b",
            content="hi",
            role="user",
            hidden=hc,
            message_type="query",
            priority=5,
            tags={"urgent"},
        )
        assert msg.receiver_id == "b"
        assert msg.role == "user"
        assert msg.hidden is hc
        assert msg.message_type == "query"
        assert msg.priority == 5
        assert msg.tags == {"urgent"}

    def test_has_hidden_true_with_state(self):
        """has_hidden is True when hidden_state is present."""
        hc = HiddenChannel(hidden_state=torch.tensor([1.0]))
        msg = Message(sender_id="a", receiver_id=None, content="x", hidden=hc)
        assert msg.has_hidden is True

    def test_has_hidden_true_with_embedding(self):
        """has_hidden is True when embedding is present."""
        hc = HiddenChannel(embedding=torch.tensor([1.0]))
        msg = Message(sender_id="a", receiver_id=None, content="x", hidden=hc)
        assert msg.has_hidden is True

    def test_has_hidden_false_no_hidden(self):
        """has_hidden is False when no hidden channel is set."""
        msg = Message(sender_id="a", receiver_id=None, content="x")
        assert msg.has_hidden is False

    def test_has_hidden_false_empty_channel(self):
        """has_hidden is False when HiddenChannel has no tensors."""
        hc = HiddenChannel()
        msg = Message(sender_id="a", receiver_id=None, content="x", hidden=hc)
        assert msg.has_hidden is False

    def test_to_visible_dict(self):
        """to_visible_dict returns only role, content, and sender."""
        msg = Message(sender_id="a", receiver_id="b", content="hello", priority=5)
        d = msg.to_visible_dict()
        assert d == {"role": "assistant", "content": "hello", "sender": "a"}
        assert "priority" not in d
        assert "receiver_id" not in d

    def test_to_full_dict_without_hidden(self):
        """to_full_dict without hidden does not include 'hidden' key."""
        msg = Message(sender_id="a", receiver_id=None, content="hi", tags={"x"})
        d = msg.to_full_dict()
        assert d["role"] == "assistant"
        assert d["content"] == "hi"
        assert d["sender"] == "a"
        assert d["message_type"] == "response"
        assert d["priority"] == 0
        assert set(d["tags"]) == {"x"}
        assert "hidden" not in d

    def test_to_full_dict_with_hidden(self):
        """to_full_dict with hidden includes the serialized channel."""
        hc = HiddenChannel(hidden_state=torch.tensor([1.0, 2.0]))
        msg = Message(sender_id="a", receiver_id=None, content="hi", hidden=hc)
        d = msg.to_full_dict()
        assert "hidden" in d
        assert d["hidden"]["hidden_state"] == [1.0, 2.0]


class TestMessageProtocol:
    """Tests for MessageProtocol."""

    def test_init_defaults(self):
        """Create with default parameters."""
        proto = MessageProtocol()
        assert proto.enable_hidden is True
        assert proto.hidden_dim is None

    def test_init_custom(self):
        """Create with custom parameters."""

        def fn(tensors):
            return tensors[0]

        proto = MessageProtocol(enable_hidden=False, hidden_dim=64, combine_hidden=fn)
        assert proto.enable_hidden is False
        assert proto.hidden_dim == 64
        assert proto.combine_hidden is fn

    def test_create_message_without_hidden(self):
        """create_message without hidden data."""
        proto = MessageProtocol()
        msg = proto.create_message("a", "hello")
        assert msg.sender_id == "a"
        assert msg.content == "hello"
        assert msg.hidden is None

    def test_create_message_with_hidden(self):
        """create_message with hidden_state."""
        proto = MessageProtocol()
        hs = torch.tensor([1.0, 2.0])
        msg = proto.create_message("a", "hi", hidden_state=hs)
        assert msg.hidden is not None
        assert msg.hidden.hidden_state is not None
        assert torch.equal(msg.hidden.hidden_state, hs)

    def test_create_message_hidden_disabled(self):
        """enable_hidden=False — hidden data is ignored."""
        proto = MessageProtocol(enable_hidden=False)
        hs = torch.tensor([1.0])
        msg = proto.create_message("a", "hi", hidden_state=hs)
        assert msg.hidden is None

    def test_create_message_with_kwargs(self):
        """create_message passes extra keyword arguments to Message."""
        proto = MessageProtocol()
        msg = proto.create_message("a", "hi", role="user", priority=3)
        assert msg.role == "user"
        assert msg.priority == 3

    def test_extract_hidden_states(self):
        """extract_hidden_states collects only non-None hidden states."""
        proto = MessageProtocol()
        hs1 = torch.tensor([1.0])
        hs2 = torch.tensor([2.0])
        msgs = [
            proto.create_message("a", "1", hidden_state=hs1),
            proto.create_message("b", "2"),
            proto.create_message("c", "3", hidden_state=hs2),
        ]
        states = proto.extract_hidden_states(msgs)
        assert len(states) == 2
        assert torch.equal(states[0], hs1)
        assert torch.equal(states[1], hs2)

    def test_extract_hidden_states_empty(self):
        """extract_hidden_states returns empty list when no hidden states exist."""
        proto = MessageProtocol()
        msgs = [proto.create_message("a", "hi")]
        assert proto.extract_hidden_states(msgs) == []

    def test_combine_incoming_hidden(self):
        """combine_incoming_hidden averages hidden states."""
        proto = MessageProtocol()
        msgs = [
            proto.create_message("a", "1", hidden_state=torch.tensor([2.0, 4.0])),
            proto.create_message("b", "2", hidden_state=torch.tensor([6.0, 8.0])),
        ]
        result = proto.combine_incoming_hidden(msgs)
        assert result is not None
        assert torch.equal(result, torch.tensor([4.0, 6.0]))

    def test_combine_incoming_hidden_none(self):
        """combine_incoming_hidden returns None when no hidden states exist."""
        proto = MessageProtocol()
        msgs = [proto.create_message("a", "hi")]
        assert proto.combine_incoming_hidden(msgs) is None

    def test_combine_incoming_hidden_custom(self):
        """Custom combine function is used when provided."""
        proto = MessageProtocol(combine_hidden=lambda ts: torch.sum(torch.stack(ts), dim=0))
        msgs = [
            proto.create_message("a", "1", hidden_state=torch.tensor([1.0, 2.0])),
            proto.create_message("b", "2", hidden_state=torch.tensor([3.0, 4.0])),
        ]
        result = proto.combine_incoming_hidden(msgs)
        assert result is not None
        assert torch.equal(result, torch.tensor([4.0, 6.0]))

    def test_default_combine_raises_on_empty(self):
        """_default_combine raises ValueError on an empty list."""
        proto = MessageProtocol()
        with pytest.raises(ValueError, match="No tensors to combine"):
            proto._default_combine([])

    def test_format_visible(self):
        """format_visible formats messages for a prompt."""
        proto = MessageProtocol()
        msgs = [
            proto.create_message("agent_a", "Hello"),
            proto.create_message("agent_b", "Reply"),
        ]
        result = proto.format_visible(msgs)
        assert "[agent_a]:" in result
        assert "Hello" in result
        assert "[agent_b]:" in result
        assert "Reply" in result

    def test_format_visible_with_names(self):
        """format_visible uses agent name mapping when provided."""
        proto = MessageProtocol()
        msgs = [proto.create_message("a", "hi")]
        result = proto.format_visible(msgs, agent_names={"a": "Voice"})
        assert "[Voice]:" in result
        assert "hi" in result

    def test_format_visible_empty(self):
        """format_visible with empty list returns empty string."""
        proto = MessageProtocol()
        assert proto.format_visible([]) == ""


class TestMemoryLevelShared:
    """Tests for MemoryLevel.SHARED behaviour in AgentMemory."""

    def test_add_shared_level_goes_to_long_term_storage(self):
        """add() with SHARED level stores the entry in _long_term internally."""
        mem = AgentMemory("a")
        entry = mem.add(content={"text": "shared_data"}, level=MemoryLevel.SHARED)

        assert entry.level == MemoryLevel.SHARED
        assert entry in mem._long_term
        assert entry not in mem._working

    def test_add_shared_level_applies_long_term_ttl(self):
        """Default TTL for SHARED level uses long_term_default_ttl."""
        cfg = MemoryConfig(long_term_default_ttl=1234.0)
        mem = AgentMemory("a", config=cfg)
        entry = mem.add(content={"text": "x"}, level=MemoryLevel.SHARED)

        assert entry.ttl == 1234.0

    def test_get_shared_level_returns_all_entries(self):
        """get(level=SHARED) falls through to the else branch and returns all entries."""
        mem = AgentMemory("a")
        mem.add(content={"i": 1})
        mem.add(content={"i": 2}, level=MemoryLevel.LONG_TERM)

        # SHARED is not WORKING and not LONG_TERM → else branch → all entries
        result = mem.get(level=MemoryLevel.SHARED)
        assert len(result) == 2

    def test_clear_shared_level_only_resets_access_counts(self):
        """
        clear(SHARED) does not remove WORKING or LONG_TERM entries,
        but does reset access counts (current implementation behaviour).
        """
        mem = AgentMemory("a")
        mem.add(content={"i": 1})
        mem.add(content={"i": 2}, level=MemoryLevel.LONG_TERM)

        mem.clear(level=MemoryLevel.SHARED)

        # Neither working nor long_term should be cleared
        assert len(mem._working) == 1
        assert len(mem._long_term) == 1

    def test_shared_memory_pool_share_creates_shared_level_entry(self):
        """SharedMemoryPool.share() without to_agents stores a SHARED-level entry."""
        pool = SharedMemoryPool()
        mem = AgentMemory("sender")
        pool.register(mem)
        entry = mem.add(content={"text": "pool_data"})
        pool.share("sender", entry)

        assert len(pool._shared_entries) == 1
        assert pool._shared_entries[0].level == MemoryLevel.SHARED


class TestIntegrationWorkingToLongTerm:
    """Entry lifecycle: working → promote → demote."""

    def test_promote_after_n_accesses(self):
        """Entry is promoted to long_term after N accesses via get()."""
        cfg = MemoryConfig(promote_after_accesses=2)
        mem = AgentMemory("a", cfg)
        mem.add(content={"text": "important"})

        assert len(mem.get(level=MemoryLevel.WORKING)) == 1
        assert len(mem.get(level=MemoryLevel.LONG_TERM)) == 0

        mem.get()

        assert len(mem.get(level=MemoryLevel.LONG_TERM)) >= 1
        lt = mem.get(level=MemoryLevel.LONG_TERM)
        assert lt[0].content == {"text": "important"}
        assert lt[0].level == MemoryLevel.LONG_TERM

    def test_demote_inactive_entry(self):
        """Inactive long-term entry is demoted back to working."""
        cfg = MemoryConfig(
            promote_after_accesses=1,
            demote_inactive_after=0.1,
            cleanup_interval=0.0,
        )
        mem = AgentMemory("a", cfg)
        mem.add(content={"text": "data"}, level=MemoryLevel.LONG_TERM)

        assert len(mem._long_term) == 1

        time.sleep(0.2)
        _ = mem.working_memory

        assert len(mem._long_term) == 0
        assert any(e.content == {"text": "data"} for e in mem._working)

    def test_full_cycle_promote_then_demote(self):
        """Working → promote → long_term → demote → working."""
        cfg = MemoryConfig(
            promote_after_accesses=2,
            demote_inactive_after=0.1,
            cleanup_interval=0.0,
        )
        mem = AgentMemory("a", cfg)
        mem.add(content={"msg": "cycle"})

        mem.get()
        mem.get()
        assert any(e.content == {"msg": "cycle"} for e in mem._long_term)

        time.sleep(0.2)
        _ = mem.working_memory

        assert any(e.content == {"msg": "cycle"} for e in mem._working)
        assert all(e.content != {"msg": "cycle"} for e in mem._long_term)


class TestIntegrationCompression:
    """Compression on working memory overflow."""

    def test_truncate_on_overflow(self):
        """Adding more than working_max_entries triggers truncation."""
        cfg = MemoryConfig(working_max_entries=5, auto_compress=True)
        mem = AgentMemory("a", cfg)

        for i in range(10):
            mem.add(content={"idx": i})

        assert len(mem._working) == 5

    def test_high_priority_survives_compression(self):
        """High-priority entries survive compression."""
        cfg = MemoryConfig(working_max_entries=3, auto_compress=True)
        mem = AgentMemory("a", cfg)

        mem.add(content={"text": "important"}, priority=10)
        for i in range(5):
            mem.add(content={"text": f"regular_{i}"}, priority=0)

        contents = [e.content["text"] for e in mem._working]
        assert "important" in contents


class TestIntegrationSharedMemoryPool:
    """SharedMemoryPool: sharing between agents."""

    def test_share_to_specific_agents(self):
        """Agent A shares an entry with agents B and C."""
        pool = SharedMemoryPool()
        mem_a = AgentMemory("a")
        mem_b = AgentMemory("b")
        mem_c = AgentMemory("c")

        pool.register(mem_a)
        pool.register(mem_b)
        pool.register(mem_c)

        entry = mem_a.add(content={"role": "assistant", "content": "answer from A"})
        pool.share("a", entry, to_agents=["b", "c"])

        b_entries = mem_b.get()
        c_entries = mem_c.get()

        assert any(e.content["content"] == "answer from A" for e in b_entries)
        assert any(e.content["content"] == "answer from A" for e in c_entries)
        assert any("shared" in e.tags for e in b_entries)

        a_entries = mem_a.get()
        shared_entries = [e for e in a_entries if "shared" in e.tags]
        assert len(shared_entries) == 0

    def test_share_to_pool_with_access_filter(self):
        """SubgraphFilter: agent from a different subgraph cannot see the entry."""
        flt = SubgraphFilter({"sg1": {"a", "b"}, "sg2": {"c"}})
        pool = SharedMemoryPool(access_filter=flt)

        mem_a = AgentMemory("a")
        mem_b = AgentMemory("b")
        mem_c = AgentMemory("c")
        pool.register(mem_a)
        pool.register(mem_b)
        pool.register(mem_c)

        entry = mem_a.add(content={"text": "secret"})
        pool.share("a", entry)

        b_shared = pool.get_shared("b")
        assert len(b_shared) == 1

        c_shared = pool.get_shared("c")
        assert len(c_shared) == 0


class TestIntegrationBroadcast:
    """Broadcast: sending to all agents."""

    def test_broadcast_reaches_all_except_sender(self):
        """Broadcast from A reaches B, C, D but not A."""
        pool = SharedMemoryPool()
        agents = {}
        for name in ["a", "b", "c", "d"]:
            mem = AgentMemory(name)
            pool.register(mem)
            agents[name] = mem

        pool.broadcast(
            from_agent="a",
            content={"role": "system", "content": "New rules"},
            tags={"instruction"},
        )

        a_entries = agents["a"].get(tags={"broadcast"})
        assert len(a_entries) == 0

        for name in ["b", "c", "d"]:
            entries = agents[name].get(tags={"broadcast"})
            assert len(entries) == 1
            assert entries[0].content["content"] == "New rules"
            assert "broadcast" in entries[0].tags
            assert "instruction" in entries[0].tags

    def test_broadcast_goes_to_working_level(self):
        """Broadcast entries land in WORKING memory."""
        pool = SharedMemoryPool()
        mem_a = AgentMemory("a")
        mem_b = AgentMemory("b")
        pool.register(mem_a)
        pool.register(mem_b)

        pool.broadcast("a", content={"text": "update"})

        b_working = mem_b.get(level=MemoryLevel.WORKING)
        assert any(e.content["text"] == "update" for e in b_working)

        b_lt = mem_b.get(level=MemoryLevel.LONG_TERM)
        assert all(e.content.get("text") != "update" for e in b_lt)

    def test_broadcast_with_no_tags(self):
        """Broadcast without tags adds only the 'broadcast' tag."""
        pool = SharedMemoryPool()
        mem_a = AgentMemory("a")
        mem_b = AgentMemory("b")
        pool.register(mem_a)
        pool.register(mem_b)

        pool.broadcast("a", content={"msg": "hello"})

        entries = mem_b.get()
        bcast = [e for e in entries if "broadcast" in e.tags]
        assert len(bcast) == 1
        assert bcast[0].tags == {"broadcast"}

    def test_broadcast_source_agent_set(self):
        """Broadcast sets source_agent on each recipient's entry."""
        pool = SharedMemoryPool()
        mem_a = AgentMemory("a")
        mem_b = AgentMemory("b")
        pool.register(mem_a)
        pool.register(mem_b)

        pool.broadcast("a", content={"msg": "hi"})

        entries = mem_b.get()
        bcast = [e for e in entries if "broadcast" in e.tags]
        assert len(bcast) == 1
        assert bcast[0].source_agent == "a"

    def test_multiple_broadcasts_accumulate(self):
        """Multiple broadcasts from different agents accumulate."""
        pool = SharedMemoryPool()
        agents = {}
        for name in ["a", "b", "c"]:
            mem = AgentMemory(name)
            pool.register(mem)
            agents[name] = mem

        pool.broadcast("a", content={"msg": "from_a"})
        pool.broadcast("b", content={"msg": "from_b"})

        c_entries = agents["c"].get(tags={"broadcast"})
        assert len(c_entries) == 2
        msgs = {e.content["msg"] for e in c_entries}
        assert msgs == {"from_a", "from_b"}

        a_entries = agents["a"].get(tags={"broadcast"})
        assert len(a_entries) == 1
        assert a_entries[0].content["msg"] == "from_b"


class TestIntegrationTTLCleanup:
    """TTL and cleanup under repeated access."""

    def test_expired_entries_not_returned(self):
        """Entries with expired TTL are not returned by get()."""
        mem = AgentMemory("a")
        mem.add(content={"text": "short-lived"}, ttl=0.1)
        mem.add(content={"text": "long-lived"}, ttl=9999)

        time.sleep(0.2)
        entries = mem.get()

        contents = [e.content["text"] for e in entries]
        assert "short-lived" not in contents
        assert "long-lived" in contents

    def test_remove_expired_cleans_up(self):
        """remove_expired() physically removes expired entries."""
        mem = AgentMemory("a")
        mem.add(content={"text": "temp"}, ttl=0.1)
        mem.add(content={"text": "perm"}, ttl=None)

        time.sleep(0.2)
        removed = mem.remove_expired()

        assert removed == 1
        assert len(mem._working) == 1
        assert mem._working[0].content["text"] == "perm"


class TestIntegrationFullPipeline:
    """Full pipeline: add → compress → promote → share → get_shared."""

    def test_end_to_end_memory_pipeline(self):
        """Entry travels the full path: add → compress → promote → share."""
        cfg = MemoryConfig(
            working_max_entries=5,
            promote_after_accesses=2,
            auto_compress=True,
        )
        pool = SharedMemoryPool()
        mem_a = AgentMemory("a", cfg)
        mem_b = AgentMemory("b")
        pool.register(mem_a)
        pool.register(mem_b)

        mem_a.add(content={"text": "key_entry"}, priority=10)
        for i in range(8):
            mem_a.add(content={"text": f"filler_{i}"}, priority=0)

        contents = [e.content["text"] for e in mem_a._working]
        assert "key_entry" in contents

        mem_a.get()
        mem_a.get()

        lt = mem_a.get(level=MemoryLevel.LONG_TERM)
        lt_texts = [e.content["text"] for e in lt]
        assert "key_entry" in lt_texts

        key_entry = next(e for e in lt if e.content["text"] == "key_entry")
        pool.share("a", key_entry, to_agents=["b"])

        b_entries = mem_b.get()
        assert any(e.content["text"] == "key_entry" for e in b_entries)


class TestLongTermCompression:
    """Tests for _maybe_compress_long_term (lines 339, 341)."""

    def test_long_term_auto_compress(self):
        """Adding entries beyond long_term_max_entries triggers compression (lines 339-341)."""
        cfg = MemoryConfig(
            long_term_max_entries=3,
            auto_compress=True,
            compression_strategy=TruncateCompressor(),
        )
        mem = AgentMemory(agent_id="test", config=cfg)

        # Add 5 long-term entries to trigger compression
        for i in range(5):
            mem.add(
                content={"text": f"entry {i}"},
                level=MemoryLevel.LONG_TERM,
            )

        # After compression, we should have at most long_term_max_entries entries
        long_term = mem.get(level=MemoryLevel.LONG_TERM)
        assert len(long_term) <= cfg.long_term_max_entries

    def test_long_term_auto_compress_disabled(self):
        """When auto_compress=False, no compression happens (line 339: return early)."""
        cfg = MemoryConfig(
            long_term_max_entries=3,
            auto_compress=False,
        )
        mem = AgentMemory(agent_id="test", config=cfg)

        for i in range(5):
            mem.add(
                content={"text": f"entry {i}"},
                level=MemoryLevel.LONG_TERM,
            )

        long_term = mem.get(level=MemoryLevel.LONG_TERM)
        assert len(long_term) == 5  # No compression


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
