"""Tests for the shared SSRF-safe HTTP helpers."""
from __future__ import annotations

import socket
import urllib.error
import urllib.parse
from unittest.mock import MagicMock

import httpx
import pytest

from readsbstats import http_safe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_addrinfo(ip: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]


def _fake_addrinfo_v6(ip: str):
    # IPv6 addrinfo tuple shape: (af, type, proto, canonname, (addr, port, flow, scope))
    return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443, 0, 0))]


def _mock_urllib_resp(body: bytes, headers: dict | None = None, url: str | None = None):
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read = MagicMock(return_value=body)
    mock.url = url
    mock.headers = headers or {}
    return mock


def _patch_validate(monkeypatch, allow=True, ip="1.1.1.1"):
    """Bypass the resolve+validate step so urllib/httpx-path tests can focus
    on the post-validation behaviour. Patches both ``validate_url`` (for
    callers that still use the old API) and ``_resolve_and_validate`` (the
    new internal helper that returns the parsed URL + addrinfo)."""
    if allow:
        monkeypatch.setattr(http_safe, "validate_url", lambda url: None)
        monkeypatch.setattr(
            http_safe, "_resolve_and_validate",
            lambda url: (
                urllib.parse.urlparse(url),
                _fake_addrinfo(ip) if ":" not in ip else _fake_addrinfo_v6(ip),
            ),
        )
    else:
        def _reject(url):
            raise ValueError("blocked by test")
        monkeypatch.setattr(http_safe, "validate_url", _reject)
        monkeypatch.setattr(http_safe, "_resolve_and_validate", _reject)


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

    # IPv6 reject branches (audit-12 #206) ---
    # Python's ipaddress.IPv6Address handles these correctly; the tests pin
    # the policy so a future refactor can't silently drop coverage for any
    # of the v6 private-address classes.

    def test_ipv6_loopback_rejected(self, monkeypatch):
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo_v6("::1"))
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://example.com/")

    def test_ipv6_link_local_rejected(self, monkeypatch):
        # fe80::/10 — link-local
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo_v6("fe80::1"))
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://example.com/")

    def test_ipv6_unique_local_rejected(self, monkeypatch):
        # fc00::/7 — unique-local (the IPv6 equivalent of RFC1918)
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo_v6("fc00::1"))
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://example.com/")

    def test_ipv6_multicast_rejected(self, monkeypatch):
        # ff00::/8 — multicast
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo_v6("ff02::1"))
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://example.com/")

    def test_ipv6_unspecified_rejected(self, monkeypatch):
        # :: — unspecified (matches IPv4 0.0.0.0)
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo_v6("::"))
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://example.com/")

    def test_ipv6_public_passes(self, monkeypatch):
        # 2001:db8::/32 is documentation space but ipaddress considers it
        # non-private. Use a well-known public address instead.
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo_v6("2606:4700:4700::1111"))
        http_safe.validate_url("https://one.one.one.one/")

    def test_ipv4_zero_zero_zero_zero_rejected(self, monkeypatch):
        # is_unspecified — was untested for IPv4 too
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("0.0.0.0"))
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://example.com/")

    def test_ipv4_rfc1918_rejected(self, monkeypatch):
        # 10.0.0.0/8 / 172.16.0.0/12 / 192.168.0.0/16 — covered indirectly
        # by photo_sources tests; pin here too so test_http_safe is the
        # authoritative reject-policy spec.
        for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                                lambda h, p, ip=ip, **kw: _fake_addrinfo(ip))
            with pytest.raises(ValueError, match="non-public"):
                http_safe.validate_url("https://example.com/")

    # PY-1 (Audit 2026-05-31): predicate must use is_global, not a
    # whitelist of negative checks. The exclusion-list approach missed
    # CGNAT (100.64/10) and benchmark (198.18/15) — neither is_private
    # nor is_reserved, but both is_global is False.

    def test_ipv4_cgnat_100_64_rejected(self, monkeypatch):
        # RFC 6598 shared address space — carrier-grade NAT, must not be
        # treated as publicly reachable.
        for ip in ("100.64.0.1", "100.127.255.255"):
            monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                                lambda h, p, ip=ip, **kw: _fake_addrinfo(ip))
            with pytest.raises(ValueError, match="non-public"):
                http_safe.validate_url("https://example.com/")

    def test_ipv4_benchmark_198_18_rejected(self, monkeypatch):
        # RFC 2544 benchmarking range — not globally routable.
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("198.18.0.1"))
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://example.com/")

    def test_ipv4_global_still_passes(self, monkeypatch):
        # Regression: the is_global tightening must not start rejecting
        # ordinary public IPs.
        for ip in ("8.8.8.8", "1.1.1.1"):
            monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                                lambda h, p, ip=ip, **kw: _fake_addrinfo(ip))
            http_safe.validate_url("https://example.com/")

    def test_ipv4_multicast_rejected(self, monkeypatch):
        # Python's ipaddress.is_global returns True for IPv4 multicast
        # (224/4). Our predicate adds `not is_multicast` so multicast
        # never becomes a valid HTTPS destination.
        for ip in ("224.0.0.1", "233.252.0.1"):
            monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                                lambda h, p, ip=ip, **kw: _fake_addrinfo(ip))
            with pytest.raises(ValueError, match="non-public"):
                http_safe.validate_url("https://example.com/")

    def test_ipv6_global_scope_multicast_rejected(self, monkeypatch):
        # Same quirk for IPv6: ff0e::1 (global-scope multicast) has
        # is_global=True. The egress policy is unicast-only.
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo_v6("ff0e::1"))
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://example.com/")

    def test_mixed_addrinfo_one_private_rejects(self, monkeypatch):
        """If getaddrinfo returns multiple records and any is private, the
        whole URL must be rejected — a partially-trusted resolver is the
        same threat shape as the rebinding attack."""
        def _gai(h, p, **kw):
            return _fake_addrinfo("8.8.8.8") + _fake_addrinfo("127.0.0.1")
        monkeypatch.setattr(http_safe, "_real_getaddrinfo", _gai)
        with pytest.raises(ValueError, match="non-public"):
            http_safe.validate_url("https://example.com/")


# ---------------------------------------------------------------------------
# DNS-rebinding TOCTOU guard — audit-12 #167–#168 + Phase 9 redesign
# ---------------------------------------------------------------------------
#
# The DNS-rebinding fix went through two iterations:
#
#   Phase 2 (v2.1.4) — process-wide ``socket.getaddrinfo`` patch checking a
#     thread-local pin set by ``validate_url``. Worked, but the patch lived
#     forever from module load, silently breaking tests that did the
#     obvious ``monkeypatch.setattr(socket, "getaddrinfo", ...)``.
#
#   Phase 9 (v2.1.11) — eliminated the global patch. urllib uses a custom
#     ``_PinnedHTTPSConnection`` that connects to the validated IP
#     directly with proper SNI. httpx uses ``_pinned_socket_resolver``,
#     a scoped context-manager that only patches ``socket.getaddrinfo``
#     for the duration of a single request.
#
# These tests verify the Phase 9 design.

class TestUrllibPinnedConnection:
    """The urllib path no longer relies on ``socket.getaddrinfo`` patching.
    Instead it builds a one-shot opener whose HTTPS handler issues every
    connection through ``_PinnedHTTPSConnection``, which uses a
    pre-validated IP."""

    def test_build_pinned_opener_wires_both_handlers(self):
        """Regression guard: a future refactor must not silently remove the
        redirect-blocking handler OR the pinned-connection handler."""
        parsed = urllib.parse.urlparse("https://example.com/")
        opener = http_safe._build_pinned_opener(parsed, "1.1.1.1", timeout=5)
        installed = {type(h).__name__ for h in opener.handlers}
        assert "_NoRedirectHandler" in installed
        assert "_PinnedHTTPSHandler" in installed

    def test_pinned_handler_uses_validated_ip_not_dns(self, monkeypatch):
        """The HTTPS handler's connection factory must produce a
        ``_PinnedHTTPSConnection`` whose ``_target_ip`` is the IP we
        passed in — NOT whatever ``socket.getaddrinfo`` would resolve
        the URL host to at fetch time."""
        parsed = urllib.parse.urlparse("https://example.com/")
        opener = http_safe._build_pinned_opener(parsed, "8.8.8.7", timeout=5)
        # Pick the pinned handler out of the opener.
        handler = next(
            h for h in opener.handlers
            if type(h).__name__ == "_PinnedHTTPSHandler"
        )
        # `_make_connection` is the factory urllib calls per request.
        conn = handler._make_connection("example.com", timeout=5)
        assert isinstance(conn, http_safe._PinnedHTTPSConnection)
        assert conn._target_ip == "8.8.8.7"
        # `host` (used for Host: header AND SNI) must remain the hostname.
        assert conn.host == "example.com"

    def test_resolve_and_validate_returns_addrinfo_for_pinning(self, monkeypatch):
        """``_resolve_and_validate`` returns ``(parsed, infos)`` — the
        urllib path picks ``infos[0][4][0]`` as the target_ip. Verify
        the contract."""
        monkeypatch.setattr(
            http_safe, "_real_getaddrinfo",
            lambda h, p, **kw: _fake_addrinfo("8.8.8.42"),
        )
        parsed, infos = http_safe._resolve_and_validate("https://example.com/")
        assert parsed.hostname == "example.com"
        assert infos[0][4][0] == "8.8.8.42"


class TestHttpxPinnedRequestBuilder:
    """Audit 2026-05-25 — the httpx path no longer mutates
    ``socket.getaddrinfo`` under a process-wide lock. Instead it rebuilds
    the URL against the pre-validated IP and uses the ``sni_hostname``
    request extension. These tests pin the new request-shape contract."""

    def test_pinned_url_uses_validated_ip(self, monkeypatch):
        _patch_validate(monkeypatch, ip="8.8.8.99")
        url, headers, ext = http_safe._build_pinned_httpx_request(
            "https://example.com/v1/widget?q=1#frag"
        )
        assert url == "https://8.8.8.99/v1/widget?q=1"  # fragment dropped
        assert headers == {"Host": "example.com"}
        assert ext == {"sni_hostname": "example.com"}

    def test_pinned_url_brackets_ipv6(self, monkeypatch):
        _patch_validate(monkeypatch, ip="2606:4700:4700::1111")
        url, _h, _e = http_safe._build_pinned_httpx_request(
            "https://example.com/path"
        )
        assert url == "https://[2606:4700:4700::1111]/path"

    def test_pinned_url_preserves_non_default_port(self, monkeypatch):
        _patch_validate(monkeypatch, ip="8.8.8.99")
        url, headers, _ext = http_safe._build_pinned_httpx_request(
            "https://example.com:8443/x"
        )
        assert url == "https://8.8.8.99:8443/x"
        assert headers == {"Host": "example.com:8443"}

    def test_concurrent_calls_do_not_share_global_state(self, monkeypatch):
        """The old `_RESOLVER_LOCK` design serialised every call. The new
        builder must be re-entrant: two threads on different hosts get
        independent (url, host, sni) triples without blocking each other."""
        from threading import Barrier, Thread
        results: list = []
        # Make the validator yield different IPs per host.
        def fake_resolve(url):
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname
            ip = {"a.example.com": "1.1.1.1",
                  "b.example.com": "2.2.2.2"}[host]
            return parsed, _fake_addrinfo(ip)
        monkeypatch.setattr(http_safe, "_resolve_and_validate", fake_resolve)

        barrier = Barrier(2)
        def worker(host: str):
            barrier.wait()  # both threads arrive at builder simultaneously
            for _ in range(50):
                url, h, e = http_safe._build_pinned_httpx_request(
                    f"https://{host}/x"
                )
                results.append((host, url, h["Host"], e["sni_hostname"]))

        threads = [Thread(target=worker, args=(h,))
                   for h in ("a.example.com", "b.example.com")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each thread should always see its own host's pin — no cross-talk.
        for host, url, hhdr, sni in results:
            ip = "1.1.1.1" if host == "a.example.com" else "2.2.2.2"
            assert ip in url
            assert hhdr == host
            assert sni == host


class TestHttpxAsyncRejection:
    """audit-12 H2 guard — async httpx bypasses ``socket.getaddrinfo``
    via ``anyio.getaddrinfo``, so our scoped pin doesn't protect it.
    Raise immediately if someone tries."""

    def test_async_client_rejected(self, monkeypatch):
        # AsyncClient may make a real network call on construction — patch
        # the resolver so we don't depend on internet.
        monkeypatch.setattr(
            http_safe, "_real_getaddrinfo",
            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"),
        )
        async_client = httpx.AsyncClient()
        try:
            with pytest.raises(RuntimeError, match="AsyncClient"):
                http_safe.safe_httpx_get(
                    async_client, "https://example.com/", max_bytes=1024,
                )
        finally:
            # AsyncClient holds a transport pool; close cleanly to avoid
            # ResourceWarning chatter.
            import asyncio
            asyncio.new_event_loop().run_until_complete(async_client.aclose())


# ---------------------------------------------------------------------------
# safe_urlopen — opener wiring + size cap
# ---------------------------------------------------------------------------

class TestSafeUrlopen:
    """Tests use ``_patch_validate`` (skip the resolver) plus a monkey-patch
    on ``_build_pinned_opener`` so we can return a mock opener. The opener's
    ``.open(req, timeout=...)`` is the only surface the helper interacts with."""

    def test_no_redirect_handler_raises_on_redirect(self):
        handler = http_safe._NoRedirectHandler()
        req = MagicMock(); req.full_url = "https://example.com/"
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(req, MagicMock(), 302, "Found", {},
                                      "https://attacker.example/")

    @staticmethod
    def _patch_opener(monkeypatch, body=b"x", headers=None, url=None):
        """Replace ``_build_pinned_opener`` with a factory that returns a
        fake opener.  Returns a list that captures every Request the
        helper passed to ``opener.open()``."""
        captured: list = []

        def fake_opener_factory(parsed, target_ip, timeout):
            class _FakeOpener:
                def open(self, req, timeout=None):
                    captured.append(req)
                    return _mock_urllib_resp(body, headers=headers, url=url)
            return _FakeOpener()

        monkeypatch.setattr(http_safe, "_build_pinned_opener", fake_opener_factory)
        return captured

    def test_returns_body_and_headers(self, monkeypatch):
        _patch_validate(monkeypatch)
        self._patch_opener(monkeypatch, body=b"hello",
                           headers={"Content-Type": "text/plain"})
        body, headers = http_safe.safe_urlopen(
            "https://example.com/", timeout=2, max_bytes=1024,
        )
        assert body == b"hello"
        assert headers["Content-Type"] == "text/plain"

    def test_oversized_response_rejected(self, monkeypatch):
        _patch_validate(monkeypatch)
        self._patch_opener(monkeypatch, body=b"x" * 2000)
        with pytest.raises(ValueError, match="max_bytes"):
            http_safe.safe_urlopen(
                "https://example.com/", timeout=2, max_bytes=1024,
            )

    def test_post_flight_url_revalidated(self, monkeypatch):
        """If the response URL differs from the request URL (a redirect
        that the no-redirect handler somehow let through), the helper
        re-runs the resolve+validate step against the final URL."""
        seen: list = []
        def _track_resolve(url):
            seen.append(url)
            return (urllib.parse.urlparse(url), _fake_addrinfo("1.1.1.1"))
        monkeypatch.setattr(http_safe, "_resolve_and_validate", _track_resolve)
        self._patch_opener(monkeypatch, body=b"data",
                           url="https://elsewhere.example/")
        http_safe.safe_urlopen(
            "https://example.com/", timeout=2, max_bytes=1024,
        )
        assert seen == ["https://example.com/", "https://elsewhere.example/"]

    def test_extra_headers_merged_with_default_user_agent(self, monkeypatch):
        _patch_validate(monkeypatch)
        captured = self._patch_opener(monkeypatch)
        http_safe.safe_urlopen(
            "https://example.com/", timeout=2, max_bytes=1024,
            extra_headers={"X-Custom": "abc"},
        )
        headers_lower = {k.lower(): v for k, v in captured[0].headers.items()}
        assert headers_lower["user-agent"].startswith("readsbstats/")
        assert headers_lower["x-custom"] == "abc"

    def test_post_data_attached_to_request(self, monkeypatch):
        """When `data=` is supplied, the urllib Request must carry it as the
        POST body so safe_urlopen can be used for the Telegram bot API
        (sendMessage / sendPhoto) — improvements.md #124."""
        _patch_validate(monkeypatch)
        captured = self._patch_opener(monkeypatch, body=b'{"ok":true}')
        body, _ = http_safe.safe_urlopen(
            "https://example.com/", timeout=2, max_bytes=1024,
            data=b'{"hello":"world"}',
            extra_headers={"Content-Type": "application/json"},
        )
        assert captured[0].data == b'{"hello":"world"}'
        assert body == b'{"ok":true}'

    def test_omitting_data_keeps_get_semantics(self, monkeypatch):
        """No `data=` → urllib Request.data is None → GET."""
        _patch_validate(monkeypatch)
        captured = self._patch_opener(monkeypatch)
        http_safe.safe_urlopen(
            "https://example.com/", timeout=2, max_bytes=1024,
        )
        assert captured[0].data is None

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

    def stream(self, method, url, **kw):
        # Audit-13 A13-016: safe_httpx_get now streams via
        # client.stream(...) and consumes resp.iter_bytes().
        self.last_kwargs = kw
        resp = self._resp
        class _Ctx:
            def __enter__(self_inner): return resp
            def __exit__(self_inner, *a): return False
        return _Ctx()


class TestSafeHttpxGet:
    def test_passes_follow_redirects_false(self, monkeypatch):
        _patch_validate(monkeypatch)
        resp = httpx.Response(200, content=b"{}",
                              request=httpx.Request("GET", "https://example.com/"))
        client = _FakeHttpxClient(resp)
        out = http_safe.safe_httpx_get(client, "https://example.com/", max_bytes=1024)
        assert out is resp
        assert client.last_kwargs["follow_redirects"] is False
        # Audit 2026-05-25 pin: SNI extension + Host override accompany the call.
        assert client.last_kwargs["headers"] == {"Host": "example.com"}
        assert client.last_kwargs["extensions"] == {"sni_hostname": "example.com"}

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
        assert client.last_kwargs["follow_redirects"] is False
        assert client.last_kwargs["timeout"] == 3.0
