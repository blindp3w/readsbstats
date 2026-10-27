"""Tests for readsbstats.mcp_server.limits (ADR-0022 Decision 4/5)."""

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from readsbstats import config
from readsbstats.mcp_server import limits
from readsbstats.mcp_server.limits import (
    Limiter, TokenBucket, check_watchlist_add, limiter_from_config,
)
from tests._helpers import run_async


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def setup():
    yield


def make_limiter(clock=None, **kw):
    args = dict(read_per_min=3, write_per_hour=2, photo_per_hour=1, concurrency=1,
                acquire_timeout=0.05)
    args.update(kw)
    if clock is not None:
        args["clock"] = clock
    return Limiter(**args)


class TestTokenBucket:
    def test_full_at_start_then_empty(self):
        clk = FakeClock()
        b = TokenBucket(3, 60, clock=clk)
        assert [b.try_take()[0] for _ in range(3)] == [True, True, True]
        ok, wait = b.try_take()
        assert not ok
        assert wait == pytest.approx(20.0)      # 60 s / 3 tokens

    def test_refill_over_time(self):
        clk = FakeClock()
        b = TokenBucket(3, 60, clock=clk)
        for _ in range(3):
            b.try_take()
        clk.t += 19
        assert not b.try_take()[0]
        clk.t += 1.5
        assert b.try_take()[0]
        assert not b.try_take()[0]

    def test_wait_shrinks_as_time_passes(self):
        clk = FakeClock()
        b = TokenBucket(1, 60, clock=clk)
        b.try_take()
        clk.t += 45
        ok, wait = b.try_take()
        assert not ok and wait == pytest.approx(15.0)

    def test_refill_capped_at_capacity(self):
        clk = FakeClock()
        b = TokenBucket(2, 60, clock=clk)
        clk.t += 10_000
        assert [b.try_take()[0] for _ in range(3)] == [True, True, False]

    def test_ok_returns_zero_wait(self):
        assert TokenBucket(1, 60, clock=FakeClock()).try_take() == (True, 0.0)


class TestLimiterCharge:
    def test_exhaustion_and_message(self):
        clk = FakeClock()
        lim = make_limiter(clk)
        for _ in range(3):
            lim.charge("alice", "read")
        with pytest.raises(ToolError, match=r"rate limit for read calls reached; retry in 20 s"):
            lim.charge("alice", "read")

    def test_retry_seconds_rounded_up(self):
        clk = FakeClock()
        lim = make_limiter(clk, read_per_min=1)
        lim.charge("a", "read")
        clk.t += 0.2
        with pytest.raises(ToolError, match=r"retry in 60 s"):    # ceil(59.8)
            lim.charge("a", "read")

    def test_clients_isolated(self):
        lim = make_limiter(FakeClock())
        for _ in range(3):
            lim.charge("alice", "read")
        lim.charge("bob", "read")          # unaffected
        with pytest.raises(ToolError):
            lim.charge("alice", "read")

    def test_kinds_independent(self):
        lim = make_limiter(FakeClock())
        lim.charge("a", "photo")
        with pytest.raises(ToolError, match="photo"):
            lim.charge("a", "photo")
        lim.charge("a", "write")
        lim.charge("a", "read")

    def test_write_window_is_hourly(self):
        clk = FakeClock()
        lim = make_limiter(clk, write_per_hour=2)
        lim.charge("a", "write")
        lim.charge("a", "write")
        with pytest.raises(ToolError, match=r"retry in 1800 s"):
            lim.charge("a", "write")
        clk.t += 1800
        lim.charge("a", "write")

    def test_refills(self):
        clk = FakeClock()
        lim = make_limiter(clk)
        for _ in range(3):
            lim.charge("a", "read")
        clk.t += 61
        lim.charge("a", "read")

    def test_unknown_kind(self):
        with pytest.raises(ValueError):
            make_limiter().charge("a", "delete")


class TestSlot:
    def test_acquire_and_release(self):
        lim = make_limiter()

        async def go():
            for _ in range(3):
                async with lim.slot():
                    pass
        run_async(go())

    def test_second_acquirer_times_out(self):
        lim = make_limiter(concurrency=1, acquire_timeout=0.05)

        async def go():
            async with lim.slot():
                with pytest.raises(ToolError, match="server busy; retry in a few seconds"):
                    async with lim.slot():
                        pytest.fail("should not enter")
        run_async(go())

    def test_concurrency_two_allows_two(self):
        lim = make_limiter(concurrency=2, acquire_timeout=0.05)

        async def go():
            async with lim.slot():
                async with lim.slot():
                    with pytest.raises(ToolError):
                        async with lim.slot():
                            pass
        run_async(go())

    def test_released_on_exception(self):
        lim = make_limiter(concurrency=1, acquire_timeout=0.05)

        async def go():
            with pytest.raises(RuntimeError):
                async with lim.slot():
                    raise RuntimeError("boom")
            async with lim.slot():     # would time out if the slot leaked
                pass
        run_async(go())

    def test_waiter_gets_slot_when_released_in_time(self):
        lim = make_limiter(concurrency=1, acquire_timeout=2.0)

        async def hold():
            async with lim.slot():
                await asyncio.sleep(0.05)

        async def go():
            t = asyncio.create_task(hold())
            await asyncio.sleep(0)
            async with lim.slot():
                pass
            await t
        run_async(go())

    def test_released_on_cancellation_while_waiting(self):
        lim = make_limiter(concurrency=1, acquire_timeout=5.0)

        async def go():
            async with lim.slot():
                waiter = asyncio.create_task(_enter(lim))
                await asyncio.sleep(0.01)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
            async with lim.slot():
                pass

        async def _enter(l):
            async with l.slot():
                pass
        run_async(go())


class TestWatchlistGuard:
    def test_short_prefix_rejected(self):
        for v in ("", "A", "AB", " AB "):
            with pytest.raises(ToolError, match="at least 3 characters"):
                check_watchlist_add("callsign_prefix", v, 0, 100)

    def test_three_char_prefix_ok(self):
        check_watchlist_add("callsign_prefix", "RYR", 0, 100)
        check_watchlist_add("callsign_prefix", " RYR ", 0, 100)

    def test_short_value_ok_for_other_types(self):
        check_watchlist_add("icao", "ab", 0, 100)
        check_watchlist_add("registration", "A", 0, 100)

    def test_full(self):
        with pytest.raises(ToolError, match=r"watchlist is full \(100 entries\); remove an entry first"):
            check_watchlist_add("icao", "aabbcc", 100, 100)
        with pytest.raises(ToolError, match="full"):
            check_watchlist_add("icao", "aabbcc", 150, 100)
        check_watchlist_add("icao", "aabbcc", 99, 100)


def test_limiter_from_config(monkeypatch):
    monkeypatch.setattr(config, "MCP_READ_PER_MIN", 1)
    monkeypatch.setattr(config, "MCP_WRITE_PER_HOUR", 2)
    monkeypatch.setattr(config, "MCP_PHOTO_PER_HOUR", 3)
    monkeypatch.setattr(config, "MCP_CONCURRENCY", 4)
    lim = limiter_from_config()
    assert isinstance(lim, Limiter)
    lim.charge("a", "read")
    with pytest.raises(ToolError):
        lim.charge("a", "read")
    for _ in range(3):
        lim.charge("a", "photo")
    with pytest.raises(ToolError):
        lim.charge("a", "photo")
