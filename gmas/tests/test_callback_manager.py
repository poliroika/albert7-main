"""Tests for src/callbacks/manager.py"""

from uuid import UUID, uuid4

import pytest

from gmas.callbacks.base import BaseCallbackHandler
from gmas.callbacks.manager import AsyncCallbackManager, CallbackManager

# ─────────────────────────── Concrete handler for testing ──────────────────────


class RecordingHandler(BaseCallbackHandler):
    """Records all calls for assertion."""

    def __init__(self, raise_error: bool = False):
        self.raise_error = raise_error
        self.calls: list[tuple[str, dict]] = []

    def _record(self, method: str, **kwargs):
        self.calls.append((method, kwargs))

    def on_run_start(self, *, run_id, query, **kwargs):
        self._record("on_run_start", run_id=run_id, query=query)

    def on_run_end(self, *, run_id, output, **kwargs):
        self._record("on_run_end", run_id=run_id, output=output)

    def on_agent_start(self, *, run_id, agent_id, **kwargs):
        self._record("on_agent_start", run_id=run_id, agent_id=agent_id)

    def on_agent_end(self, *, run_id, agent_id, output, **kwargs):
        self._record("on_agent_end", run_id=run_id, agent_id=agent_id)

    def on_agent_error(self, error, *, run_id, agent_id, **kwargs):
        self._record("on_agent_error", run_id=run_id, agent_id=agent_id)

    def on_retry(self, *, run_id, agent_id, attempt, **kwargs):
        self._record("on_retry", run_id=run_id, agent_id=agent_id)

    def on_llm_new_token(self, token, *, run_id, agent_id, **kwargs):
        self._record("on_llm_new_token", run_id=run_id, agent_id=agent_id, token=token)

    def on_plan_created(self, *, run_id, num_steps, execution_order, **kwargs):
        self._record("on_plan_created", run_id=run_id, num_steps=num_steps)

    def on_topology_changed(self, *, run_id, reason, **kwargs):
        self._record("on_topology_changed", run_id=run_id, reason=reason)

    def on_prune(self, *, run_id, agent_id, reason, **kwargs):
        self._record("on_prune", run_id=run_id, agent_id=agent_id)

    def on_fallback(self, *, run_id, failed_agent_id, fallback_agent_id, **kwargs):
        self._record("on_fallback", run_id=run_id, failed_agent_id=failed_agent_id)

    def on_parallel_start(self, *, run_id, agent_ids, **kwargs):
        self._record("on_parallel_start", run_id=run_id, agent_ids=agent_ids)

    def on_parallel_end(self, *, run_id, agent_ids, **kwargs):
        self._record("on_parallel_end", run_id=run_id, agent_ids=agent_ids)

    def on_tool_start(self, *, run_id, tool_name, **kwargs):
        self._record("on_tool_start", run_id=run_id, tool_name=tool_name)

    def on_tool_end(self, *, run_id, tool_name, **kwargs):
        self._record("on_tool_end", run_id=run_id, tool_name=tool_name)

    def on_memory_read(self, *, run_id, agent_id, **kwargs):
        self._record("on_memory_read", run_id=run_id)

    def on_memory_write(self, *, run_id, agent_id, key, **kwargs):
        self._record("on_memory_write", run_id=run_id, key=key)

    def on_budget_warning(self, *, run_id, budget_type, **kwargs):
        self._record("on_budget_warning", run_id=run_id, budget_type=budget_type)

    def on_budget_exceeded(self, *, run_id, budget_type, **kwargs):
        self._record("on_budget_exceeded", run_id=run_id, budget_type=budget_type)

    def on_tool_error(self, *, run_id, tool_name, **kwargs):
        self._record("on_tool_error", run_id=run_id, tool_name=tool_name)


class ErrorHandler(RecordingHandler):
    """Handler that raises on all calls."""

    raise_error = True

    def on_run_start(self, *, run_id, query, **kwargs):
        msg = "intentional error"
        raise RuntimeError(msg)


# ═══════════════════════════════════════════════════════════════
#  CallbackManager initialization & configuration
# ═══════════════════════════════════════════════════════════════


class TestCallbackManagerInit:
    def test_empty_init(self):
        cm = CallbackManager()
        assert cm.handlers == []
        assert cm.tags == []
        assert cm.metadata == {}
        assert cm.is_async is False

    def test_with_handlers(self):
        h = RecordingHandler()
        cm = CallbackManager(handlers=[h])
        assert h in cm.handlers

    def test_with_tags_and_metadata(self):
        cm = CallbackManager(tags=["t1", "t2"], metadata={"key": "value"})
        assert "t1" in cm.tags
        assert cm.metadata["key"] == "value"

    def test_configure_classmethod(self):
        h = RecordingHandler()
        cm = CallbackManager.configure(
            handlers=[h],
            tags=["tag"],
            metadata={"k": "v"},
        )
        assert h in cm.handlers
        assert "tag" in cm.tags

    def test_copy(self):
        h = RecordingHandler()
        cm = CallbackManager(handlers=[h], tags=["t1"])
        copy = cm.copy()
        assert h in copy.handlers
        assert copy is not cm

    def test_merge(self):
        h1 = RecordingHandler()
        h2 = RecordingHandler()
        cm1 = CallbackManager(handlers=[h1], tags=["t1"])
        cm2 = CallbackManager(handlers=[h2], tags=["t2"])
        merged = cm1.merge(cm2)
        assert h1 in merged.handlers
        assert h2 in merged.handlers
        assert "t1" in merged.tags
        assert "t2" in merged.tags

    def test_add_handler(self):
        cm = CallbackManager()
        h = RecordingHandler()
        cm.add_handler(h)
        assert h in cm.handlers

    def test_add_handler_inheritable(self):
        cm = CallbackManager()
        h = RecordingHandler()
        cm.add_handler(h, inherit=True)
        assert h in cm.handlers
        assert h in cm.inheritable_handlers

    def test_remove_handler(self):
        h = RecordingHandler()
        cm = CallbackManager(handlers=[h])
        cm.remove_handler(h)
        assert h not in cm.handlers

    def test_set_handlers(self):
        h1 = RecordingHandler()
        h2 = RecordingHandler()
        cm = CallbackManager(handlers=[h1])
        cm.set_handlers([h2])
        assert cm.handlers == [h2]

    def test_add_tags(self):
        cm = CallbackManager()
        cm.add_tags(["t1", "t2"])
        assert "t1" in cm.tags

    def test_add_tags_inheritable(self):
        cm = CallbackManager()
        cm.add_tags(["t1"], inherit=True)
        assert "t1" in cm.inheritable_tags

    def test_remove_tags(self):
        cm = CallbackManager(tags=["t1", "t2"])
        cm.remove_tags(["t1"])
        assert "t1" not in cm.tags
        assert "t2" in cm.tags

    def test_add_metadata(self):
        cm = CallbackManager()
        cm.add_metadata({"key": "val"})
        assert cm.metadata["key"] == "val"

    def test_add_metadata_inheritable(self):
        cm = CallbackManager()
        cm.add_metadata({"key": "val"}, inherit=True)
        assert cm.inheritable_metadata["key"] == "val"

    def test_get_child(self):
        h = RecordingHandler()
        cm = CallbackManager(inheritable_handlers=[h], inheritable_tags=["t"])
        parent_run_id = uuid4()
        child = cm.get_child(parent_run_id)
        assert h in child.handlers
        assert "t" in child.tags
        assert child.parent_run_id == parent_run_id


# ═══════════════════════════════════════════════════════════════
#  CallbackManager event dispatching
# ═══════════════════════════════════════════════════════════════


class TestCallbackManagerEvents:
    def setup_method(self):
        self.handler = RecordingHandler()
        self.cm = CallbackManager(handlers=[self.handler])
        self.run_id = uuid4()

    def test_on_run_start_returns_run_id(self):
        rid = self.cm.on_run_start(query="test", num_agents=3)
        assert isinstance(rid, UUID)

    def test_on_run_start_with_provided_id(self):
        rid = self.cm.on_run_start(run_id=self.run_id, query="test")
        assert rid == self.run_id

    def test_on_run_start_dispatched(self):
        self.cm.on_run_start(query="hello")
        assert any(m == "on_run_start" for m, _ in self.handler.calls)

    def test_on_run_end_dispatched(self):
        self.cm.on_run_end(self.run_id, output="done", success=True)
        assert any(m == "on_run_end" for m, _ in self.handler.calls)

    def test_on_agent_start_dispatched(self):
        self.cm.on_agent_start(self.run_id, agent_id="solver")
        assert any(m == "on_agent_start" for m, _ in self.handler.calls)

    def test_on_agent_end_dispatched(self):
        self.cm.on_agent_end(self.run_id, agent_id="solver", output="result")
        assert any(m == "on_agent_end" for m, _ in self.handler.calls)

    def test_on_agent_error_dispatched(self):
        self.cm.on_agent_error(self.run_id, ValueError("err"), agent_id="solver")
        assert any(m == "on_agent_error" for m, _ in self.handler.calls)

    def test_on_retry_dispatched(self):
        self.cm.on_retry(self.run_id, agent_id="solver", attempt=1)
        assert any(m == "on_retry" for m, _ in self.handler.calls)

    def test_on_llm_new_token_dispatched(self):
        self.cm.on_llm_new_token(self.run_id, "tok", agent_id="solver")
        assert any(m == "on_llm_new_token" for m, _ in self.handler.calls)

    def test_on_plan_created_dispatched(self):
        self.cm.on_plan_created(
            self.run_id,
            num_steps=3,
            execution_order=["a", "b", "c"],
        )
        assert any(m == "on_plan_created" for m, _ in self.handler.calls)

    def test_on_topology_changed_dispatched(self):
        self.cm.on_topology_changed(
            self.run_id,
            reason="pruned",
            old_remaining=["a", "b"],
            new_remaining=["b"],
        )
        assert any(m == "on_topology_changed" for m, _ in self.handler.calls)

    def test_on_prune_dispatched(self):
        self.cm.on_prune(self.run_id, agent_id="solver", reason="low quality")
        assert any(m == "on_prune" for m, _ in self.handler.calls)

    def test_on_fallback_dispatched(self):
        self.cm.on_fallback(
            self.run_id,
            failed_agent_id="solver",
            fallback_agent_id="backup",
        )
        assert any(m == "on_fallback" for m, _ in self.handler.calls)

    def test_on_parallel_start_dispatched(self):
        self.cm.on_parallel_start(self.run_id, agent_ids=["a", "b"])
        assert any(m == "on_parallel_start" for m, _ in self.handler.calls)

    def test_on_parallel_end_dispatched(self):
        self.cm.on_parallel_end(self.run_id, agent_ids=["a", "b"])
        assert any(m == "on_parallel_end" for m, _ in self.handler.calls)

    def test_on_tool_start_dispatched(self):
        self.cm.on_tool_start(self.run_id, agent_id="solver", tool_name="search", action="search")
        assert any(m == "on_tool_start" for m, _ in self.handler.calls)

    def test_on_tool_end_dispatched(self):
        self.cm.on_tool_end(self.run_id, agent_id="solver", tool_name="search", success=True)
        assert any(m == "on_tool_end" for m, _ in self.handler.calls)

    def test_on_memory_read_dispatched(self):
        self.cm.on_memory_read(self.run_id, agent_id="solver", keys=["context"])
        assert any(m == "on_memory_read" for m, _ in self.handler.calls)

    def test_on_memory_write_dispatched(self):
        self.cm.on_memory_write(self.run_id, agent_id="solver", key="context", value_size=256)
        assert any(m == "on_memory_write" for m, _ in self.handler.calls)

    def test_on_budget_warning_dispatched(self):
        self.cm.on_budget_warning(
            self.run_id,
            budget_type="tokens",
            current=800,
            limit=1000,
            ratio=0.8,
        )
        assert any(m == "on_budget_warning" for m, _ in self.handler.calls)

    def test_on_budget_exceeded_dispatched(self):
        self.cm.on_budget_exceeded(self.run_id, budget_type="requests", current=10, limit=10)
        assert any(m == "on_budget_exceeded" for m, _ in self.handler.calls)

    def test_on_tool_error_dispatched(self):
        self.cm.on_tool_error(
            self.run_id,
            agent_id="solver",
            tool_name="search",
            error_type="timeout",
            error_message="timed out",
        )
        assert any(m == "on_tool_error" for m, _ in self.handler.calls)

    def test_ignore_memory_skips_handler(self):
        class MemIgnoringHandler(RecordingHandler):
            ignore_memory = True

        handler = MemIgnoringHandler()
        cm = CallbackManager(handlers=[handler])
        run_id = uuid4()
        cm.on_memory_read(run_id, agent_id="solver")
        cm.on_memory_write(run_id, agent_id="solver", key="k")
        assert not any(m in ("on_memory_read", "on_memory_write") for m, _ in handler.calls)

    def test_ignore_budget_skips_handler(self):
        class BudgetIgnoringHandler(RecordingHandler):
            ignore_budget = True

        handler = BudgetIgnoringHandler()
        cm = CallbackManager(handlers=[handler])
        run_id = uuid4()
        cm.on_budget_warning(run_id, budget_type="tokens", current=800, limit=1000)
        assert not any(m == "on_budget_warning" for m, _ in handler.calls)

    def test_ignore_tool_skips_handler(self):
        class ToolIgnoringHandler(RecordingHandler):
            ignore_tool = True

        handler = ToolIgnoringHandler()
        cm = CallbackManager(handlers=[handler])
        run_id = uuid4()
        cm.on_tool_start(run_id, tool_name="search")
        assert not any(m == "on_tool_start" for m, _ in handler.calls)

    def test_ignore_retry_skips_handler(self):
        class RetryIgnoringHandler(RecordingHandler):
            ignore_retry = True

        handler = RetryIgnoringHandler()
        cm = CallbackManager(handlers=[handler])
        run_id = uuid4()
        cm.on_retry(run_id, agent_id="solver", attempt=1)
        assert not any(m == "on_retry" for m, _ in handler.calls)

    def test_ignore_llm_skips_handler(self):
        class LLMIgnoringHandler(RecordingHandler):
            ignore_llm = True

        handler = LLMIgnoringHandler()
        cm = CallbackManager(handlers=[handler])
        run_id = uuid4()
        cm.on_llm_new_token(run_id, "tok", agent_id="solver")
        assert not any(m == "on_llm_new_token" for m, _ in handler.calls)


# ═══════════════════════════════════════════════════════════════
#  Error handling
# ═══════════════════════════════════════════════════════════════


class TestCallbackManagerErrorHandling:
    def test_handler_error_propagated_when_raise_error(self):
        handler = ErrorHandler(raise_error=True)
        cm = CallbackManager(handlers=[handler])
        with pytest.raises(RuntimeError, match="intentional error"):
            cm.on_run_start(query="test")

    def test_handler_error_suppressed_when_not_raise_error(self):
        class SilentErrorHandler(RecordingHandler):
            raise_error = False

            def on_run_start(self, *, run_id, query, **kwargs):
                msg = "silent error"
                raise ValueError(msg)

        handler = SilentErrorHandler()
        cm = CallbackManager(handlers=[handler])
        # Should not raise
        cm.on_run_start(query="test")

    def test_ignore_agent_skips_handlers(self):
        class IgnoringHandler(RecordingHandler):
            ignore_agent = True

        handler = IgnoringHandler()
        cm = CallbackManager(handlers=[handler])
        run_id = uuid4()
        cm.on_run_start(query="test")
        cm.on_agent_start(run_id, agent_id="solver")
        # Both on_run_start and on_agent_start should be skipped
        assert len(handler.calls) == 0

    def test_multiple_handlers(self):
        h1 = RecordingHandler()
        h2 = RecordingHandler()
        cm = CallbackManager(handlers=[h1, h2])
        cm.on_run_start(query="test")
        assert any(m == "on_run_start" for m, _ in h1.calls)
        assert any(m == "on_run_start" for m, _ in h2.calls)


# ═══════════════════════════════════════════════════════════════
#  AsyncCallbackManager
# ═══════════════════════════════════════════════════════════════


class TestAsyncCallbackManager:
    def test_is_async(self):
        acm = AsyncCallbackManager()
        assert acm.is_async is True

    def test_init(self):
        h = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[h])
        assert h in acm.handlers

    def test_configure(self):
        h = RecordingHandler()
        acm = AsyncCallbackManager.configure(handlers=[h])
        assert h in acm.handlers

    @pytest.mark.asyncio
    async def test_on_run_start_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        rid = await acm.on_run_start(query="test async")
        assert isinstance(rid, UUID)

    @pytest.mark.asyncio
    async def test_on_run_end_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_run_end(run_id, output="done")
        assert any(m == "on_run_end" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_agent_start_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_agent_start(run_id, agent_id="solver")
        assert any(m == "on_agent_start" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_agent_end_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_agent_end(run_id, agent_id="solver", output="result")
        assert any(m == "on_agent_end" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_plan_created_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_plan_created(run_id, num_steps=2, execution_order=["a", "b"])
        assert any(m == "on_plan_created" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_agent_error_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_agent_error(run_id, ValueError("err"), agent_id="solver")
        assert any(m == "on_agent_error" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_retry_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_retry(run_id, agent_id="solver", attempt=1, max_attempts=3)
        assert any(m == "on_retry" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_llm_new_token_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_llm_new_token(run_id, "tok", agent_id="solver")
        assert any(m == "on_llm_new_token" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_topology_changed_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_topology_changed(run_id, reason="pruned", old_remaining=["a"], new_remaining=[])
        assert any(m == "on_topology_changed" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_prune_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_prune(run_id, agent_id="solver", reason="low quality")
        assert any(m == "on_prune" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_fallback_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_fallback(run_id, failed_agent_id="solver", fallback_agent_id="backup")
        assert any(m == "on_fallback" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_parallel_start_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_parallel_start(run_id, agent_ids=["a", "b"])
        assert any(m == "on_parallel_start" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_parallel_end_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_parallel_end(run_id, agent_ids=["a", "b"], successful=["a"], failed=["b"])
        assert any(m == "on_parallel_end" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_memory_read_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_memory_read(run_id, agent_id="solver", keys=["ctx"])
        assert any(m == "on_memory_read" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_memory_write_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_memory_write(run_id, agent_id="solver", key="result", value_size=128)
        assert any(m == "on_memory_write" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_budget_warning_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_budget_warning(run_id, budget_type="tokens", current=800, limit=1000)
        assert any(m == "on_budget_warning" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_budget_exceeded_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_budget_exceeded(run_id, budget_type="requests", current=10, limit=10)
        assert any(m == "on_budget_exceeded" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_tool_start_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_tool_start(run_id, agent_id="solver", tool_name="search")
        assert any(m == "on_tool_start" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_tool_end_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_tool_end(run_id, agent_id="solver", tool_name="search", success=True)
        assert any(m == "on_tool_end" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_on_tool_error_async(self):
        handler = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_tool_error(run_id, agent_id="solver", tool_name="search", error_type="timeout")
        assert any(m == "on_tool_error" for m, _ in handler.calls)

    def test_copy(self):
        h = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[h], tags=["t1"])
        copy = acm.copy()
        assert h in copy.handlers
        assert copy is not acm

    def test_merge(self):
        h1 = RecordingHandler()
        h2 = RecordingHandler()
        acm1 = AsyncCallbackManager(handlers=[h1], tags=["t1"])
        acm2 = AsyncCallbackManager(handlers=[h2], tags=["t2"])
        merged = acm1.merge(acm2)
        assert h1 in merged.handlers
        assert h2 in merged.handlers

    def test_add_handler(self):
        acm = AsyncCallbackManager()
        h = RecordingHandler()
        acm.add_handler(h)
        assert h in acm.handlers

    def test_add_handler_inheritable(self):
        acm = AsyncCallbackManager()
        h = RecordingHandler()
        acm.add_handler(h, inherit=True)
        assert h in acm.inheritable_handlers

    def test_remove_handler(self):
        h = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[h])
        acm.remove_handler(h)
        assert h not in acm.handlers

    def test_set_handlers(self):
        h1 = RecordingHandler()
        h2 = RecordingHandler()
        acm = AsyncCallbackManager(handlers=[h1])
        acm.set_handlers([h2])
        assert acm.handlers == [h2]

    def test_add_tags(self):
        acm = AsyncCallbackManager()
        acm.add_tags(["t1", "t2"])
        assert "t1" in acm.tags

    def test_add_tags_inheritable(self):
        acm = AsyncCallbackManager()
        acm.add_tags(["t1"], inherit=True)
        assert "t1" in acm.inheritable_tags

    def test_remove_tags(self):
        acm = AsyncCallbackManager(tags=["t1", "t2"])
        acm.remove_tags(["t1"])
        assert "t1" not in acm.tags

    def test_add_metadata(self):
        acm = AsyncCallbackManager()
        acm.add_metadata({"key": "val"})
        assert acm.metadata["key"] == "val"

    def test_add_metadata_inheritable(self):
        acm = AsyncCallbackManager()
        acm.add_metadata({"key": "val"}, inherit=True)
        assert acm.inheritable_metadata["key"] == "val"

    def test_get_child(self):
        h = RecordingHandler()
        acm = AsyncCallbackManager(inheritable_handlers=[h], inheritable_tags=["t"])
        parent_run_id = uuid4()
        child = acm.get_child(parent_run_id)
        assert h in child.handlers
        assert child.parent_run_id == parent_run_id

    @pytest.mark.asyncio
    async def test_ignore_memory_skips_handler(self):
        class MemoryIgnoringHandler(RecordingHandler):
            ignore_memory = True

        handler = MemoryIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_memory_read(run_id, agent_id="solver")
        await acm.on_memory_write(run_id, agent_id="solver", key="k")
        # Memory events should be skipped
        assert not any(m in ("on_memory_read", "on_memory_write") for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_ignore_budget_skips_handler(self):
        class BudgetIgnoringHandler(RecordingHandler):
            ignore_budget = True

        handler = BudgetIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_budget_warning(run_id, budget_type="tokens", current=800, limit=1000)
        assert not any(m == "on_budget_warning" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_ignore_tool_skips_handler(self):
        class ToolIgnoringHandler(RecordingHandler):
            ignore_tool = True

        handler = ToolIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        run_id = uuid4()
        await acm.on_tool_start(run_id, tool_name="search")
        assert not any(m == "on_tool_start" for m, _ in handler.calls)

    @pytest.mark.asyncio
    async def test_error_in_async_handler_is_handled(self):
        class ErrorAsyncHandler(RecordingHandler):
            raise_error = False

            def on_run_start(self, *, run_id, query, **kwargs):
                msg = "async error"
                raise RuntimeError(msg)

        handler = ErrorAsyncHandler()
        acm = AsyncCallbackManager(handlers=[handler])
        # Should not raise due to raise_error=False
        await acm.on_run_start(query="test")


# ═══════════════════════════════════════════════════════════════
#  Additional ignore_* branch coverage tests
# ═══════════════════════════════════════════════════════════════


class TestCallbackManagerIgnoreBranchCoverage:
    """Tests that exercise the ignore_* continue branches in CallbackManager."""

    def test_remove_handler_from_inheritable_handlers(self):
        """Line 113: remove from inheritable_handlers."""
        h = RecordingHandler()
        cm = CallbackManager()
        cm.add_handler(h, inherit=True)
        assert h in cm.inheritable_handlers
        cm.remove_handler(h)
        assert h not in cm.inheritable_handlers

    def test_ignore_agent_skips_on_run_end(self):
        """Line 206: ignore_agent continue in on_run_end."""

        class AgentIgnoringHandler(RecordingHandler):
            ignore_agent = True

        h = AgentIgnoringHandler()
        cm = CallbackManager(handlers=[h])
        cm.on_run_end(uuid4(), output="done", success=True)
        assert not any(m == "on_run_end" for m, _ in h.calls)

    def test_ignore_agent_skips_on_agent_end(self):
        """Line 267: ignore_agent continue in on_agent_end."""

        class AgentIgnoringHandler(RecordingHandler):
            ignore_agent = True

        h = AgentIgnoringHandler()
        cm = CallbackManager(handlers=[h])
        cm.on_agent_end(uuid4(), agent_id="solver", output="result")
        assert not any(m == "on_agent_end" for m, _ in h.calls)

    def test_ignore_agent_skips_on_agent_error(self):
        """Line 298: ignore_agent continue in on_agent_error."""

        class AgentIgnoringHandler(RecordingHandler):
            ignore_agent = True

        h = AgentIgnoringHandler()
        cm = CallbackManager(handlers=[h])
        cm.on_agent_error(uuid4(), ValueError("err"), agent_id="solver")
        assert not any(m == "on_agent_error" for m, _ in h.calls)

    def test_ignore_budget_skips_on_budget_exceeded(self):
        """Line 600: ignore_budget continue in on_budget_exceeded."""

        class BudgetIgnoringHandler(RecordingHandler):
            ignore_budget = True

        h = BudgetIgnoringHandler()
        cm = CallbackManager(handlers=[h])
        cm.on_budget_exceeded(uuid4(), budget_type="requests", current=10, limit=10)
        assert not any(m == "on_budget_exceeded" for m, _ in h.calls)

    def test_ignore_tool_skips_on_tool_end(self):
        """Line 657: ignore_tool continue in on_tool_end."""

        class ToolIgnoringHandler(RecordingHandler):
            ignore_tool = True

        h = ToolIgnoringHandler()
        cm = CallbackManager(handlers=[h])
        cm.on_tool_end(uuid4(), agent_id="solver", tool_name="search", success=True)
        assert not any(m == "on_tool_end" for m, _ in h.calls)

    def test_ignore_tool_skips_on_tool_error(self):
        """Line 687: ignore_tool continue in on_tool_error."""

        class ToolIgnoringHandler(RecordingHandler):
            ignore_tool = True

        h = ToolIgnoringHandler()
        cm = CallbackManager(handlers=[h])
        cm.on_tool_error(uuid4(), agent_id="solver", tool_name="search", error_type="timeout", error_message="err")
        assert not any(m == "on_tool_error" for m, _ in h.calls)


class TestAsyncCallbackManagerIgnoreBranchCoverage:
    """Tests that exercise the ignore_* continue branches in AsyncCallbackManager."""

    def test_remove_handler_from_inheritable_handlers(self):
        """Line 782: remove from inheritable_handlers in AsyncCallbackManager."""
        h = RecordingHandler()
        acm = AsyncCallbackManager()
        acm.add_handler(h, inherit=True)
        assert h in acm.inheritable_handlers
        acm.remove_handler(h)
        assert h not in acm.inheritable_handlers

    def test_handle_error_raises_when_raise_error(self):
        """Line 820: AsyncCallbackManager._handle_error raises if raise_error=True."""
        h = RecordingHandler()
        h.raise_error = True
        acm = AsyncCallbackManager()
        err = RuntimeError("intentional raise")
        with pytest.raises(RuntimeError, match="intentional raise"):
            acm._handle_error(h, "on_run_start", err)

    @pytest.mark.asyncio
    async def test_ignore_agent_skips_on_run_start_async(self):
        """Line 856: ignore_agent continue in AsyncCallbackManager.on_run_start."""

        class AgentIgnoringHandler(RecordingHandler):
            ignore_agent = True

        h = AgentIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[h])
        await acm.on_run_start(query="test")
        assert not any(m == "on_run_start" for m, _ in h.calls)

    @pytest.mark.asyncio
    async def test_ignore_agent_skips_on_run_end_async(self):
        """Line 890: ignore_agent continue in AsyncCallbackManager.on_run_end."""

        class AgentIgnoringHandler(RecordingHandler):
            ignore_agent = True

        h = AgentIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[h])
        await acm.on_run_end(uuid4(), output="done")
        assert not any(m == "on_run_end" for m, _ in h.calls)

    @pytest.mark.asyncio
    async def test_ignore_agent_skips_on_agent_start_async(self):
        """Line 925: ignore_agent continue in AsyncCallbackManager.on_agent_start."""

        class AgentIgnoringHandler(RecordingHandler):
            ignore_agent = True

        h = AgentIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[h])
        await acm.on_agent_start(uuid4(), agent_id="solver")
        assert not any(m == "on_agent_start" for m, _ in h.calls)

    @pytest.mark.asyncio
    async def test_ignore_agent_skips_on_agent_end_async(self):
        """Line 959: ignore_agent continue in AsyncCallbackManager.on_agent_end."""

        class AgentIgnoringHandler(RecordingHandler):
            ignore_agent = True

        h = AgentIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[h])
        await acm.on_agent_end(uuid4(), agent_id="solver", output="result")
        assert not any(m == "on_agent_end" for m, _ in h.calls)

    @pytest.mark.asyncio
    async def test_ignore_agent_skips_on_agent_error_async(self):
        """Line 994: ignore_agent continue in AsyncCallbackManager.on_agent_error."""

        class AgentIgnoringHandler(RecordingHandler):
            ignore_agent = True

        h = AgentIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[h])
        await acm.on_agent_error(uuid4(), ValueError("err"), agent_id="solver")
        assert not any(m == "on_agent_error" for m, _ in h.calls)

    @pytest.mark.asyncio
    async def test_ignore_retry_skips_on_retry_async(self):
        """Line 1029: ignore_retry continue in AsyncCallbackManager.on_retry."""

        class RetryIgnoringHandler(RecordingHandler):
            ignore_retry = True

        h = RetryIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[h])
        await acm.on_retry(uuid4(), agent_id="solver", attempt=1, max_attempts=3)
        assert not any(m == "on_retry" for m, _ in h.calls)

    @pytest.mark.asyncio
    async def test_ignore_llm_skips_on_llm_new_token_async(self):
        """Line 1063: ignore_llm continue in AsyncCallbackManager.on_llm_new_token."""

        class LLMIgnoringHandler(RecordingHandler):
            ignore_llm = True

        h = LLMIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[h])
        await acm.on_llm_new_token(uuid4(), "tok", agent_id="solver")
        assert not any(m == "on_llm_new_token" for m, _ in h.calls)

    @pytest.mark.asyncio
    async def test_ignore_budget_skips_on_budget_exceeded_async(self):
        """Line 1338: ignore_budget continue in AsyncCallbackManager.on_budget_exceeded."""

        class BudgetIgnoringHandler(RecordingHandler):
            ignore_budget = True

        h = BudgetIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[h])
        await acm.on_budget_exceeded(uuid4(), budget_type="requests", current=10, limit=10)
        assert not any(m == "on_budget_exceeded" for m, _ in h.calls)

    @pytest.mark.asyncio
    async def test_ignore_tool_skips_on_tool_end_async(self):
        """Line 1403: ignore_tool continue in AsyncCallbackManager.on_tool_end."""

        class ToolIgnoringHandler(RecordingHandler):
            ignore_tool = True

        h = ToolIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[h])
        await acm.on_tool_end(uuid4(), agent_id="solver", tool_name="search", success=True)
        assert not any(m == "on_tool_end" for m, _ in h.calls)

    @pytest.mark.asyncio
    async def test_ignore_tool_skips_on_tool_error_async(self):
        """Line 1437: ignore_tool continue in AsyncCallbackManager.on_tool_error."""

        class ToolIgnoringHandler(RecordingHandler):
            ignore_tool = True

        h = ToolIgnoringHandler()
        acm = AsyncCallbackManager(handlers=[h])
        await acm.on_tool_error(
            uuid4(), agent_id="solver", tool_name="search", error_type="timeout", error_message="err"
        )
        assert not any(m == "on_tool_error" for m, _ in h.calls)
