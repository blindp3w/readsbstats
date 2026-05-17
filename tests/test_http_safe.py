"""Tests for the shared SSRF-safe HTTP helpers."""
from __future__ import annotations

import socket
import urllib.error
from unittest.mock import MagicMock

import httpx
import pytest

from readsbstats import http_safe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_addrinfo(ip: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]


def _mock_urllib_resp(body: bytes, headers: dict | None = None, url: str | None = None):
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read = MagicMock(return_value=body)
    mock.url = url
    mock.headers = headers or {}
    return mock


def _patch_validate(monkeypatch, allow=True):
    if allow:
        monkeypatch.setattr(http_safe, "validate_url", lambda url: None)
    else:
        def _reject(url):
            raise ValueError("blocked by test")
        monkeypatch.setattr(http_safe, "validate_url", _reject)


# ---------------------------------------------------------------------------
# validate_url
# ---------------------------------------------------------------------------

class TestValidateUrl:
    def test_https_public_ip_passes(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        http_safe.validate_url("https://example.com/")

    def test_http_rejected(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        with pytest.raises(ValueError, match="non-https"):
            http_safe.validate_url("http://example.com/")

    def test_loopback_rejected(self, monkeypatch):
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("127.0.0.1"))
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://localhost/")

    def test_metadata_169_254_rejected(self, monkeypatch):
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("169.254.169.254"))
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://meta/")

    def test_dns_failure(self, monkeypatch):
        def _gai(*a, **kw): raise socket.gaierror("no such host")
        monkeypatch.setattr(http_safe, "_real_getaddrinfo", _gai)
        with pytest.raises(ValueError, match="DNS resolution failed"):
            http_safe.validate_url("https://nope.invalid/")


# ---------------------------------------------------------------------------
# DNS-rebinding TOCTOU guard (audit-12 #167–#168)
# ---------------------------------------------------------------------------

class TestDnsPinning:
    """validate_url() must pin the resolved IPs so the subsequent fetch
    cannot resolve to a different (private) IP via DNS rebinding."""

    def test_validate_url_pins_resolved_addrinfo(self, monkeypatch):
        """After validate_url succeeds, _pinned_getaddrinfo must return the
        validated infos for the same host (so the fetch cannot re-resolve)."""
        # Real resolver returns public IP first
        call_count = {"n": 0}
        def _gai(host, port, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _fake_addrinfo("1.1.1.1")
            # Subsequent calls would return a private IP (the rebinding attack)
            return _fake_addrinfo("127.0.0.1")
        monkeypatch.setattr(http_safe, "_real_getaddrinfo", _gai)

        try:
            http_safe.validate_url("https://example.com/")
            # Now the next socket.getaddrinfo call should return the pinned
            # public IP, not the rebinding private IP.
            infos = socket.getaddrinfo("example.com", 443)
            ips = [info[4][0] for info in infos]
            assert ips == ["1.1.1.1"], (
                f"DNS rebinding not blocked — got {ips}, expected pinned "
                "['1.1.1.1']"
            )
            # The real resolver was called once (during validate_url) and
            # the second lookup hit the pin without going through _real_getaddrinfo.
            assert call_count["n"] == 1
        finally:
            http_safe._clear_dns_pin()

    def test_pin_does_not_leak_to_unrelated_hostnames(self, monkeypatch):
        """Pinning example.com must not break resolution of other.example.org."""
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        try:
            http_safe.validate_url("https://example.com/")
            # Unrelated hostname must still pass through to real resolver
            infos = socket.getaddrinfo("other.example.org", 443)
            assert infos[0][4][0] == "1.1.1.1"
        finally:
            http_safe._clear_dns_pin()

    def test_clear_dns_pin_removes_thread_local_state(self, monkeypatch):
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        http_safe.validate_url("https://example.com/")
        http_safe._clear_dns_pin()
        # After clear, the pin map is empty (lookup falls through to real)
        # Re-monkeypatch real to a sentinel and verify it gets called
        sentinel_called = {"n": 0}
        def _sentinel(h, p, **kw):
            sentinel_called["n"] += 1
            return _fake_addrinfo("8.8.8.8")
        monkeypatch.setattr(http_safe, "_real_getaddrinfo", _sentinel)
        infos = socket.getaddrinfo("example.com", 443)
        assert sentinel_called["n"] == 1
        assert infos[0][4][0] == "8.8.8.8"

    def test_dns_pin_is_thread_local(self, monkeypatch):
        """A pin set in one thread must not be visible in another."""
        import threading
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))

        # Pin in main thread
        http_safe.validate_url("https://example.com/")
        try:
            results = {}

            def worker():
                # Inside this thread, no pin is set — should call through to
                # _real_getaddrinfo (our monkey-patched one).
                seen = {"n": 0}
                def _track(h, p, **kw):
                    seen["n"] += 1
                    return _fake_addrinfo("9.9.9.9")
                # Override real for just this lookup
                orig = http_safe._real_getaddrinfo
                http_safe._real_getaddrinfo = _track
                try:
                    results["info"] = socket.getaddrinfo("example.com", 443)
                    results["calls"] = seen["n"]
                finally:
                    http_safe._real_getaddrinfo = orig

            t = threading.Thread(target=worker)
            t.start()
            t.join()

            # Worker thread had no pin → real getaddrinfo was called
            assert results["calls"] == 1
            assert results["info"][0][4][0] == "9.9.9.9"
        finally:
            http_safe._clear_dns_pin()


# ---------------------------------------------------------------------------
# safe_urlopen — opener wiring + size cap
# ---------------------------------------------------------------------------

class TestSafeUrlopen:
    def test_no_redirect_handler_is_wired(self):
        """Regression guard: a future refactor must not silently remove the
        redirect-blocking handler from the opener chain."""
        installed = [type(h).__name__ for h in http_safe._no_redirect_opener.handlers]
        assert "_NoRedirectHandler" in installed

    def test_no_redirect_handler_raises_on_redirect(self):
        handler = http_safe._NoRedirectHandler()
        req = MagicMock(); req.full_url = "https://example.com/"
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(req, MagicMock(), 302, "Found", {},
                                      "https://attacker.example/")

    def test_returns_body_and_headers(self, monkeypatch):
        _patch_validate(monkeypatch)
        monkeypatch.setattr(
            http_safe._no_redirect_opener, "open",
            lambda req, timeout=None: _mock_urllib_resp(
                b"hello", headers={"Content-Type": "text/plain"},
            ),
        )
        body, headers = http_safe.safe_urlopen(
            "https://example.com/", timeout=2, max_bytes=1024,
        )
        assert body == b"hello"
        assert headers["Content-Type"] == "text/plain"

    def test_oversized_response_rejected(self, monkeypatch):
        _patch_validate(monkeypatch)
        monkeypatch.setattr(
            http_safe._no_redirect_opener, "open",
            lambda req, timeout=None: _mock_urllib_resp(b"x" * 2000),
        )
        with pytest.raises(ValueError, match="max_bytes"):
            http_safe.safe_urlopen(
                "https://example.com/", timeout=2, max_bytes=1024,
            )

    def test_post_flight_url_revalidated(self, monkeypatch):
        seen = []
        monkeypatch.setattr(http_safe, "validate_url",
                            lambda url: seen.append(url))
        monkeypatch.setattr(
            http_safe._no_redirect_opener, "open",
            lambda req, timeout=None: _mock_urllib_resp(
                b"data", url="https://elsewhere.example/",
            ),
        )
        http_safe.safe_urlopen(
            "https://example.com/", timeout=2, max_bytes=1024,
        )
        assert seen == ["https://example.com/", "https://elsewhere.example/"]

    def test_extra_headers_merged_with_default_user_agent(self, monkeypatch):
        _patch_validate(monkeypatch)
        captured = []
        def fake_open(req, timeout=None):
            captured.append(dict(req.headers))
            return _mock_urllib_resp(b"x")
        monkeypatch.setattr(http_safe._no_redirect_opener, "open", fake_open)
        http_safe.safe_urlopen(
            "https://example.com/", timeout=2, max_bytes=1024,
            extra_headers={"X-Custom": "abc"},
        )
        # urllib title-cases header names
        headers_lower = {k.lower(): v for k, v in captured[0].items()}
        assert headers_lower["user-agent"].startswith("readsbstats/")
        assert headers_lower["x-custom"] == "abc"

    def test_post_data_attached_to_request(self, monkeypatch):
        """When `data=` is supplied, the urllib Request must carry it as the
        POST body so safe_urlopen can be used for the Telegram bot API
        (sendMessage / sendPhoto) — improvements.md #124."""
        _patch_validate(monkeypatch)
        captured = []
        def fake_open(req, timeout=None):
            captured.append(req.data)
            return _mock_urllib_resp(b'{"ok":true}')
        monkeypatch.setattr(http_safe._no_redirect_opener, "open", fake_open)
        body, _ = http_safe.safe_urlopen(
            "https://example.com/", timeout=2, max_bytes=1024,
            data=b'{"hello":"world"}',
            extra_headers={"Content-Type": "application/json"},
        )
        assert captured == [b'{"hello":"world"}']
        assert body == b'{"ok":true}'

    def test_omitting_data_keeps_get_semantics(self, monkeypatch):
        """No `data=` → urllib Request.data is None → GET."""
        _patch_validate(monkeypatch)
        captured = []
        def fake_open(req, timeout=None):
            captured.append(req.data)
            return _mock_urllib_resp(b"x")
        monkeypatch.setattr(http_safe._no_redirect_opener, "open", fake_open)
        http_safe.safe_urlopen(
            "https://example.com/", timeout=2, max_bytes=1024,
        )
        assert captured == [None]

    def test_post_still_rejects_http(self, monkeypatch):
        """POST policy must not weaken HTTPS enforcement."""
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        with pytest.raises(ValueError, match="non-https"):
            http_safe.safe_urlopen(
                "http://example.com/", timeout=2, max_bytes=1024,
                data=b"payload",
            )


# ---------------------------------------------------------------------------
# safe_httpx_get — redirect blocking + size cap
# ---------------------------------------------------------------------------

class _FakeHttpxClient:
    def __init__(self, response): self._resp = response; self.last_kwargs = None
    def get(self, url, **kw):
        self.last_kwargs = kw
        return self._resp


class TestSafeHttpxGet:
    def test_passes_follow_redirects_false(self, monkeypatch):
        _patch_validate(monkeypatch)
        resp = httpx.Response(200, content=b"{}",
                              request=httpx.Request("GET", "https://example.com/"))
        client = _FakeHttpxClient(resp)
        out = http_safe.safe_httpx_get(client, "https://example.com/", max_bytes=1024)
        assert out is resp
        assert client.last_kwargs == {"follow_redirects": False}

    def test_302_raises(self, monkeypatch):
        _patch_validate(monkeypatch)
        resp = httpx.Response(302, headers={"Location": "https://attacker.example/"},
                              request=httpx.Request("GET", "https://example.com/"))
        with pytest.raises(ValueError, match="redirect"):
            http_safe.safe_httpx_get(_FakeHttpxClient(resp),
                                      "https://example.com/", max_bytes=1024)

    def test_oversized_response_raises(self, monkeypatch):
        _patch_validate(monkeypatch)
        resp = httpx.Response(200, content=b"x" * 2000,
                              request=httpx.Request("GET", "https://example.com/"))
        with pytest.raises(ValueError, match="max_bytes"):
            http_safe.safe_httpx_get(_FakeHttpxClient(resp),
                                      "https://example.com/", max_bytes=1024)

    def test_non_https_rejected_before_get(self, monkeypatch):
        # leave validate_url un-patched so it really runs
        called = []
        client = _FakeHttpxClient(None)
        # If get were called we'd notice
        class TripClient:
            def get(self, *a, **kw): called.append(1); return None
        with pytest.raises(ValueError, match="non-https"):
            http_safe.safe_httpx_get(TripClient(), "http://example.com/", max_bytes=1024)
        assert called == []

    def test_timeout_kwarg_forwarded(self, monkeypatch):
        _patch_validate(monkeypatch)
        resp = httpx.Response(200, content=b"{}",
                              request=httpx.Request("GET", "https://example.com/"))
        client = _FakeHttpxClient(resp)
        http_safe.safe_httpx_get(client, "https://example.com/",
                                  max_bytes=1024, timeout=3.0)
        assert client.last_kwargs == {"follow_redirects": False, "timeout": 3.0}
