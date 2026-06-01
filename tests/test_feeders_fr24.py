"""Regression for W-2 (Audit 2026-06-01): FR24 feeder fetch must stream the
response body with a hard size cap.

`_feeder_details_fr24` legitimately bypasses `http_safe.safe_httpx_get`
(the helper is HTTPS-only; FR24's monitor.json is loopback http://). The
defect is the missing size cap — a misbehaving local FR24 daemon could
return an unbounded body and OOM the uvicorn worker. The fix streams via
client.stream(...) + aiter_bytes() and aborts mid-stream once 256 KB is
crossed, mirroring the pattern in http_safe.safe_httpx_get.
"""
from __future__ import annotations

import asyncio
import json

import httpx as _httpx
import pytest

from readsbstats.api import feeders


class _FakeStreamResp:
    """Stub of the object yielded by httpx.AsyncClient.stream(...).

    Provides raise_for_status() and aiter_bytes(). The test sets `chunks`
    to control what the iterator yields; `iter_count` records how many
    chunks were actually consumed so we can assert mid-stream cutoff.
    """

    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.iter_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def aiter_bytes(self):
        for c in self.chunks:
            self.iter_count += 1
            yield c


class _FakeClient:
    def __init__(self, resp: _FakeStreamResp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method: str, url: str):
        assert method == "GET"
        return self._resp


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestFR24StreamingCap:
    def test_oversize_body_aborts_mid_stream(self, monkeypatch):
        # 10 chunks of 64 KB = 640 KB total — well over the 256 KB cap.
        chunks = [b"x" * (64 * 1024) for _ in range(10)]
        resp = _FakeStreamResp(chunks)
        monkeypatch.setattr(_httpx, "AsyncClient", lambda **kw: _FakeClient(resp))

        result = _run(feeders._feeder_details_fr24("http://127.0.0.1/monitor.json"))
        assert result == []
        # Two conditions together prove "mid-stream cutoff":
        #  * iter_count > 0 → streaming actually happened (rules out today's
        #    `client.get()` path, which would never consume the iterator)
        #  * iter_count <= 5 → loop stopped no later than chunk 5 (320 KB),
        #    which is the first chunk that pushes past the 256 KB cap.
        assert resp.iter_count > 0, "expected streaming, not buffered get()"
        assert resp.iter_count <= 5, (
            f"expected mid-stream cutoff, but {resp.iter_count} chunks were consumed"
        )

    def test_under_cap_parses_normally(self, monkeypatch):
        payload = {
            "build_version": "1.2.3",
            "feed_status": "connected",
            "feed_alias": "T-KZXX1",
            "feed_num_ac_tracked": 42,
            "rx_connected": "1",
            "mlat-ok": "1",
        }
        body = json.dumps(payload).encode()
        # Two chunks summing to a small body, well under 256 KB.
        chunks = [body[: len(body) // 2], body[len(body) // 2 :]]
        resp = _FakeStreamResp(chunks)
        monkeypatch.setattr(_httpx, "AsyncClient", lambda **kw: _FakeClient(resp))

        result = _run(feeders._feeder_details_fr24("http://127.0.0.1/monitor.json"))
        labels = {k for k, _ in result}
        assert {"Version", "FR24 link", "Radar code", "Aircraft tracked", "Receiver", "MLAT"} <= labels
        assert any(v == "connected" for k, v in result if k == "Receiver")
        # All chunks must have been consumed for a normal parse.
        assert resp.iter_count == 2

    def test_network_error_returns_empty(self, monkeypatch):
        class _Boom:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def stream(self, *a, **kw):
                raise ConnectionError("down")

        monkeypatch.setattr(_httpx, "AsyncClient", lambda **kw: _Boom())
        result = _run(feeders._feeder_details_fr24("http://127.0.0.1/monitor.json"))
        assert result == []

    def test_max_bytes_constant_is_256kb(self):
        # Lock the cap value so a future "let's just bump it a bit" change
        # surfaces in code review.
        assert feeders._FR24_MAX_BYTES == 256 * 1024
