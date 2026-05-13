"""Tests for src/utils/async_utils.py — run_sync, gather_with_concurrency, timeout_wrapper."""

import asyncio

import pytest

from gmas.utils.async_utils import gather_with_concurrency, run_sync, timeout_wrapper


class TestRunSync:
    def test_run_simple_coroutine(self):
        async def add(a, b):
            return a + b

        result = run_sync(add(2, 3))
        assert result == 5

    def test_run_awaitable_result(self):
        async def greeting():
            return "hello"

        assert run_sync(greeting()) == "hello"

    def test_raises_in_running_loop(self):
        async def check():
            # Inside a running loop, run_sync should raise.
            # The coroutine must be explicitly closed afterwards because
            # run_sync raises before ever scheduling it, which would
            # otherwise cause "coroutine 'sleep' was never awaited".
            coro = asyncio.sleep(0)
            try:
                with pytest.raises(RuntimeError, match="event loop"):
                    run_sync(coro)
            finally:
                coro.close()

        asyncio.run(check())

    def test_run_sync_with_non_coroutine_awaitable(self):
        """Cover lines 19-22: run_sync with non-coroutine awaitable (has __await__ but not a coroutine)."""

        class EagerAwaitable:
            """Simple awaitable that immediately returns a value."""

            def __init__(self, value):
                self.value = value

            def __await__(self):
                yield from []  # immediate resolution, no suspension
                return self.value

        result = run_sync(EagerAwaitable(99))
        assert result == 99


class TestGatherWithConcurrency:
    async def test_basic_gather(self):
        async def double(x):
            return x * 2

        results = await gather_with_concurrency(3, double(1), double(2), double(3))
        assert sorted(results) == [2, 4, 6]

    async def test_concurrency_limit_one(self):
        """With limit=1, tasks run sequentially."""
        order = []

        async def task(n):
            order.append(f"start-{n}")
            await asyncio.sleep(0.01)
            order.append(f"end-{n}")
            return n

        results = await gather_with_concurrency(1, task(1), task(2), task(3))
        assert sorted(results) == [1, 2, 3]

    async def test_empty_coros(self):
        results = await gather_with_concurrency(5)
        assert results == []

    async def test_concurrency_n_larger_than_tasks(self):
        async def noop():
            return True

        results = await gather_with_concurrency(100, noop(), noop(), noop())
        assert results == [True, True, True]

    async def test_exception_propagates(self):
        async def faulty():
            msg = "test error"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="test error"):
            await gather_with_concurrency(2, faulty())


class TestTimeoutWrapper:
    async def test_succeeds_within_timeout(self):
        async def fast():
            await asyncio.sleep(0.01)
            return "ok"

        result = await timeout_wrapper(fast(), timeout_seconds=5.0)
        assert result == "ok"

    async def test_raises_timeout_error(self):
        async def slow():
            await asyncio.sleep(10.0)
            return "never"

        with pytest.raises(TimeoutError, match="timed out"):
            await timeout_wrapper(slow(), timeout_seconds=0.05, error_message="Operation timed out")

    async def test_custom_error_message(self):
        async def slow():
            await asyncio.sleep(10.0)

        with pytest.raises(TimeoutError, match="custom message"):
            await timeout_wrapper(slow(), timeout_seconds=0.05, error_message="custom message")

    async def test_returns_value_on_success(self):
        async def compute():
            return 42

        result = await timeout_wrapper(compute(), timeout_seconds=1.0)
        assert result == 42
