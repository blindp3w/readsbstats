"""Per-client rate limits and a global concurrency cap (ADR-0022, Decisions 4-5)."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from contextlib import asynccontextmanager

from mcp.server.mcpserver.exceptions import ToolError

from .. import config

_MIN_PREFIX_LEN = 3


class TokenBucket:
    """Starts full; refills *capacity* tokens every *per_seconds*."""

    def __init__(self, capacity: int, per_seconds: float, clock=time.monotonic):
        self.capacity = float(capacity)
        self._rate = capacity / per_seconds
        self._clock = clock
        self._tokens = self.capacity
        self._last = clock()
        self._lock = threading.Lock()

    def try_take(self) -> tuple[bool, float]:
        """Take one token. Returns (ok, seconds until the next token)."""
        with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True, 0.0
            return False, (1.0 - self._tokens) / self._rate


class Limiter:
    def __init__(self, *, read_per_min: int, write_per_hour: int, photo_per_hour: int,
                 concurrency: int, acquire_timeout: float = 5.0, clock=time.monotonic):
        self._spec = {
            "read": (read_per_min, 60.0),
            "write": (write_per_hour, 3600.0),
            "photo": (photo_per_hour, 3600.0),
        }
        self._clock = clock
        self._buckets: dict[tuple[str, str], TokenBucket] = {}
        self._lock = threading.Lock()
        self._sem = asyncio.Semaphore(concurrency)
        self._acquire_timeout = acquire_timeout

    def charge(self, client_name: str, kind: str) -> None:
        if kind not in self._spec:
            raise ValueError(f"unknown rate-limit kind {kind!r}")
        with self._lock:
            bucket = self._buckets.get((client_name, kind))
            if bucket is None:
                cap, per = self._spec[kind]
                bucket = self._buckets[(client_name, kind)] = TokenBucket(cap, per, self._clock)
        ok, wait = bucket.try_take()
        if not ok:
            raise ToolError(f"rate limit for {kind} calls reached; retry in {math.ceil(wait)} s")

    @asynccontextmanager
    async def slot(self):
        """Hold one of the global API-call slots for the duration of the block."""
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._acquire_timeout)
        except asyncio.TimeoutError:
            raise ToolError("server busy; retry in a few seconds") from None
        try:
            yield
        finally:
            self._sem.release()


def check_watchlist_add(match_type: str, value: str, current_count: int, max_entries: int) -> None:
    if match_type == "callsign_prefix" and len(value.strip()) < _MIN_PREFIX_LEN:
        raise ToolError("callsign_prefix must be at least 3 characters "
                        "(shorter prefixes match too many flights)")
    if current_count >= max_entries:
        raise ToolError(f"watchlist is full ({max_entries} entries); remove an entry first")


def limiter_from_config() -> Limiter:
    return Limiter(read_per_min=config.MCP_READ_PER_MIN,
                   write_per_hour=config.MCP_WRITE_PER_HOUR,
                   photo_per_hour=config.MCP_PHOTO_PER_HOUR,
                   concurrency=config.MCP_CONCURRENCY)
