"""Tests for the shared SSRF-safe HTTP helpers."""
from __future__ import annotations

import socket
import urllib.error
import urllib.parse
from unittest.mock import MagicMock

import httpx
import pytest

from readsbstats import http_safe
from tests._helpers import run_async


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

    def test_no_host_rejected(self):
        with pytest.raises(http_safe.UnsafeURLError, match="no host"):
            http_safe.validate_url("https:///")

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

    # SEC-1 (audit 18): IPv6-transition prefixes that embed/route to
    # private space. ipaddress.is_global flags these as global (correct
    # per IANA), but they are an SSRF bypass: a NAT64 / 6to4 / Teredo /
    # site-local address can carry traffic to an internal v4 destination
    # such as the cloud metadata service. The policy blanket-rejects the
    # whole transition prefixes via `_ip_is_public`.

    @pytest.mark.parametrize("bad", ["", "not-an-ip", "1.2.3", "1.2.3.4.5",
                                     "999.1.1.1", "1.1.1.1/24", "zzzz::1"])
    def test_ip_is_public_malformed_literal_is_false(self, bad):
        """An unparseable address must be treated as non-public (fail closed),
        not raise ValueError out of the egress policy."""
        assert http_safe._ip_is_public(bad) is False

    def test_ip_is_public_accepts_ordinary_public_addresses(self):
        # Counterpart guard so the fail-closed branch can't swallow everything.
        assert http_safe._ip_is_public("8.8.8.8") is True
        assert http_safe._ip_is_public("2606:4700:4700::1111") is True

    def test_ip_is_public_rejects_nat64_embedded_metadata(self):
        # 64:ff9b::/96 well-known NAT64 prefix embedding 169.254.169.254.
        assert http_safe._ip_is_public("64:ff9b::169.254.169.254") is False

    def test_ip_is_public_rejects_site_local(self):
        # fec0::/10 deprecated site-local (RFC 3879).
        assert http_safe._ip_is_public("fec0::1") is False

    def test_ip_is_public_rejects_nat64_local_use(self):
        # 64:ff9b:1::/48 local-use NAT64 (RFC 8215).
        assert http_safe._ip_is_public("64:ff9b:1::1") is False

    def test_ip_is_public_rejects_6to4(self):
        # 2002::/16 6to4 — 2002:a9fe:a9fe:: routes to 169.254.169.254.
        assert http_safe._ip_is_public("2002:a9fe:a9fe::1") is False

    def test_ip_is_public_rejects_teredo(self):
        # 2001::/32 Teredo tunnelling.
        assert http_safe._ip_is_public("2001:0:53aa:64c:0:0:0:1") is False

    def test_ip_is_public_existing_v6_rejects_still_hold(self):
        # Regression: the new deny-net check must not weaken the existing
        # private/loopback/link-local/ULA/multicast rejects.
        for ip in ("::1", "fe80::1", "fc00::1", "ff02::1", "::"):
            assert http_safe._ip_is_public(ip) is False

    def test_ip_is_public_existing_v4_rejects_still_hold(self):
        for ip in ("169.254.169.254", "127.0.0.1", "10.0.0.1", "0.0.0.0"):
            assert http_safe._ip_is_public(ip) is False

    def test_ip_is_public_normal_addresses_still_pass(self):
        # The transition-prefix deny list must not start rejecting ordinary
        # public v4/v6 addresses.
        for ip in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            assert http_safe._ip_is_public(ip) is True

    # SEC-3 (audit 18): URLs with embedded credentials must be rejected.
    # A `https://user:pass@host/` URL otherwise (a) leaks the userinfo into
    # the Host header on the httpx path and (b) is a phishing/confusion
    # vector. Reject it like any other policy violation.

    def test_credentialed_url_rejected(self, monkeypatch):
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        with pytest.raises(http_safe.UnsafeURLError, match="credential"):
            http_safe.validate_url("https://user:pass@example.com/x")

    def test_username_only_url_rejected(self, monkeypatch):
        monkeypatch.setattr(http_safe, "_real_getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        with pytest.raises(http_safe.UnsafeURLError, match="credential"):
            http_safe.validate_url("https://admin@example.com/x")

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

    def test_connect_dials_pinned_ip_with_hostname_sni(self, monkeypatch):
        """``connect()`` must TCP-connect to the pinned IP (no DNS) and wrap the
        socket with the ORIGINAL hostname as SNI / cert-verification name."""
        raw_sock, tls_sock = object(), object()
        dialled = []

        def fake_create_connection(address, timeout=None, **kw):
            dialled.append((address, timeout))
            return raw_sock

        def no_dns(*a, **k):
            raise AssertionError("connect() must not resolve DNS")

        monkeypatch.setattr(http_safe.socket, "create_connection", fake_create_connection)
        monkeypatch.setattr(http_safe.socket, "getaddrinfo", no_dns)
        ctx = MagicMock()
        ctx.wrap_socket.return_value = tls_sock

        conn = http_safe._PinnedHTTPSConnection(
            hostname="example.com", target_ip="8.8.8.7", port=8443,
            timeout=7.5, context=ctx)
        conn.connect()

        assert dialled == [(("8.8.8.7", 8443), 7.5)]
        ctx.wrap_socket.assert_called_once_with(raw_sock, server_hostname="example.com")
        assert conn.sock is tls_sock

    def test_pinned_opener_sends_request_through_pinned_connection(self, monkeypatch):
        """End-to-end through urllib: the handler's ``https_open`` must route the
        request via the pinned connection (dial the pinned IP, Host header = the
        URL hostname) and return the parsed response."""
        import io
        import urllib.request

        sent = []

        class _TlsSock:
            def sendall(self, data):
                sent.append(bytes(data))
            def makefile(self, *a, **k):
                return io.BytesIO(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            def close(self):
                pass

        dialled = []
        monkeypatch.setattr(
            http_safe.socket, "create_connection",
            lambda address, timeout=None, **kw: dialled.append(address) or object())
        ctx = MagicMock()
        ctx.wrap_socket.return_value = _TlsSock()
        handler = http_safe._PinnedHTTPSHandler(
            hostname="example.com", target_ip="8.8.8.7", port=443,
            timeout=5, context=ctx)
        opener = urllib.request.build_opener(handler)

        with opener.open("https://example.com/path") as resp:
            assert resp.status == 200
            assert resp.read() == b"ok"

        assert dialled == [("8.8.8.7", 443)]
        ctx.wrap_socket.assert_called_once()
        assert ctx.wrap_socket.call_args.kwargs["server_hostname"] == "example.com"
        request_bytes = b"".join(sent)
        assert request_bytes.startswith(b"GET /path HTTP/1.1\r\n")
        assert b"\r\nHost: example.com\r\n" in request_bytes

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

    def test_host_header_excludes_userinfo(self, monkeypatch):
        """SEC-3 (audit 18): the Host header is built from hostname[:port],
        never ``parsed.netloc`` — so even if a credentialed URL reached the
        builder, the ``user:pass@`` must not leak into the Host header.

        ``_resolve_and_validate`` now rejects credentialed URLs outright, so
        we patch it here to return a parsed result that still carries the
        userinfo, proving the Host-header construction itself strips it."""
        def fake_resolve(url):
            return urllib.parse.urlparse(url), _fake_addrinfo("8.8.8.99")
        monkeypatch.setattr(http_safe, "_resolve_and_validate", fake_resolve)
        _url, headers, _ext = http_safe._build_pinned_httpx_request(
            "https://user:pass@example.com/x"
        )
        assert headers == {"Host": "example.com"}

    def test_host_header_brackets_ipv6_literal_url(self, monkeypatch):
        """Code-review follow-up to SEC-3: for a URL whose host is an IPv6
        literal, parsed.hostname strips the brackets, so the Host header must
        re-add them to stay well-formed (a bare ``2606:..`` Host is malformed)."""
        _patch_validate(monkeypatch, ip="2606:4700:4700::1111")
        _url, headers, _ext = http_safe._build_pinned_httpx_request(
            "https://[2606:4700:4700::1111]/path"
        )
        assert headers == {"Host": "[2606:4700:4700::1111]"}

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
            run_async(async_client.aclose())


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

    def test_json_readable_after_streamed_response(self, monkeypatch):
        """Audit 17: safe_httpx_get consumes the body via iter_bytes() (to abort
        mid-stream at max_bytes) and patches resp._content so callers — the
        route/adsbx enrichers — can still call .json()/.content afterward. Pin
        that contract; it guards a future httpx bump that might rename _content."""
        _patch_validate(monkeypatch)
        resp = httpx.Response(200, content=b'{"hello": "world"}',
                              request=httpx.Request("GET", "https://example.com/"))
        out = http_safe.safe_httpx_get(_FakeHttpxClient(resp),
                                       "https://example.com/", max_bytes=1024)
        assert out.json() == {"hello": "world"}
        assert out.content == b'{"hello": "world"}'

    def test_302_raises(self, monkeypatch):
        _patch_validate(monkeypatch)
        resp = httpx.Response(302, headers={"Location": "https://attacker.example/"},
                              request=httpx.Request("GET", "https://example.com/"))
        with pytest.raises(ValueError, match="redirect"):
            http_safe.safe_httpx_get(_FakeHttpxClient(resp),
                                      "https://example.com/", max_bytes=1024)

    def test_followed_redirect_history_blocked(self, monkeypatch):
        """Defense-in-depth mirror of the urllib path's final-URL re-validation:
        a 200 that arrived via a redirect chain (non-empty .history) is rejected,
        so the httpx path can't silently lose protection if follow_redirects is
        ever enabled. Audit 2026-06-20."""
        _patch_validate(monkeypatch)
        resp = httpx.Response(
            200, content=b"{}",
            request=httpx.Request("GET", "https://example.com/"),
            history=[httpx.Response(302, request=httpx.Request("GET", "https://example.com/"))],
        )
        with pytest.raises(ValueError, match="redirect"):
            http_safe.safe_httpx_get(_FakeHttpxClient(resp),
                                      "https://example.com/", max_bytes=1024)

    # -- compatibility branch: clients exposing only .get() (no .stream()) ----

    def test_compat_get_only_client_returns_response(self, monkeypatch):
        _patch_validate(monkeypatch)
        resp = httpx.Response(200, content=b'{"ok": true}',
                              request=httpx.Request("GET", "https://example.com/"))

        class _GetOnly:
            last_kwargs = None
            def get(self, url, **kw):
                self.last_kwargs = kw
                return resp

        client = _GetOnly()
        assert not hasattr(client, "stream")
        out = http_safe.safe_httpx_get(client, "https://example.com/", max_bytes=1024)
        assert out is resp
        assert client.last_kwargs["follow_redirects"] is False

    def test_compat_get_only_client_followed_redirect_history_blocked(self, monkeypatch):
        """Same defence-in-depth as the streaming branch, on the .get()-only
        path (this is the branch test_followed_redirect_history_blocked does NOT
        reach, because _FakeHttpxClient has .stream())."""
        _patch_validate(monkeypatch)
        resp = httpx.Response(
            200, content=b"{}",
            request=httpx.Request("GET", "https://example.com/"),
            history=[httpx.Response(302, request=httpx.Request("GET", "https://example.com/"))],
        )

        class _GetOnly:
            def get(self, url, **kw):
                return resp

        with pytest.raises(http_safe.UnsafeURLError, match="redirect chain blocked"):
            http_safe.safe_httpx_get(_GetOnly(), "https://example.com/", max_bytes=1024)

    def test_compat_get_only_client_redirect_status_and_size_cap(self, monkeypatch):
        _patch_validate(monkeypatch)
        req = httpx.Request("GET", "https://example.com/")

        class _GetOnly:
            def __init__(self, resp): self._resp = resp
            def get(self, url, **kw): return self._resp

        redirect = httpx.Response(302, headers={"Location": "https://evil.example/"},
                                  request=req)
        with pytest.raises(http_safe.UnsafeURLError, match="redirect blocked"):
            http_safe.safe_httpx_get(_GetOnly(redirect), "https://example.com/",
                                     max_bytes=1024)
        big = httpx.Response(200, content=b"x" * 2000, request=req)
        with pytest.raises(http_safe.UnsafeURLError, match="max_bytes"):
            http_safe.safe_httpx_get(_GetOnly(big), "https://example.com/",
                                     max_bytes=1024)

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


# ---------------------------------------------------------------------------
# is_loopback_http_url / loopback_request — loopback-only helper for our own
# local services (e.g. the MCP server -> 127.0.0.1:8080)
# ---------------------------------------------------------------------------

class TestIsLoopbackHttpUrl:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/api/stats?x=1&y=2",
        "http://localhost:8080/api/flights",
        "http://[::1]:8080/api/health",
        "http://[::1]/",
        "http://127.0.0.1",
    ])
    def test_allowed(self, url):
        assert http_safe.is_loopback_http_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://127.0.0.1/",
        "ftp://127.0.0.1/",
        "file:///etc/passwd",
        "http://u:p@127.0.0.1/",
        "http://u@127.0.0.1/",
        "http://127.0.0.1@evil.com/",
        "http://evil.com@127.0.0.1/",
        "http://127.0.0.2/",
        "http://0.0.0.0/",
        "http://localhost.evil.com/",
        "http://evil.com/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://017700000001/",
        "http://127.1/",
        "http://127.0.0.1:99999/",
        "http://127.0.0.1:abc/",
        "http://127.0.0.1 /",
        "http://127.0.0.1\\@evil.com/",
        "//127.0.0.1/",
        "127.0.0.1",
        "",
        None,
        123,
    ])
    def test_rejected(self, url):
        assert http_safe.is_loopback_http_url(url) is False

    def test_loopback_hosts_constant(self):
        assert http_safe.LOOPBACK_HOSTS == ("127.0.0.1", "localhost", "::1")


class TestLoopbackRequest:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.calls: list[httpx.Request] = []
        self._clients: list[httpx.AsyncClient] = []
        yield
        for c in self._clients:
            run_async(c.aclose())

    def _client(self, handler):
        async def wrapped(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return handler(request)

        c = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
        self._clients.append(c)
        return c

    def _call(self, handler, method="GET", url="http://127.0.0.1:8080/api/x",
              max_bytes=1024, timeout=2.0, **kw):
        client = self._client(handler)
        return run_async(http_safe.loopback_request(
            client, method, url, max_bytes=max_bytes, timeout=timeout, **kw,
        ))

    # -- URL policy ---------------------------------------------------------

    @pytest.mark.parametrize("url", [
        "http://u:p@127.0.0.1/",
        "https://127.0.0.1/",
        "ftp://127.0.0.1/",
        "http://127.0.0.2/",
        "http://0.0.0.0/",
        "http://localhost.evil.com/",
        "http://evil.com/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://127.0.0.1@evil.com/",
    ])
    def test_rejected_urls_never_reach_transport(self, url):
        with pytest.raises(http_safe.UnsafeURLError):
            self._call(lambda r: httpx.Response(200, content=b"x"), url=url)
        assert self.calls == []

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8080/a?b=1",
        "http://localhost:8080/a",
        "http://[::1]:8080/a",
    ])
    def test_allowed_urls_reach_transport(self, url):
        status, _h, body = self._call(
            lambda r: httpx.Response(200, content=b"ok"), url=url,
        )
        assert (status, body) == (200, b"ok")
        assert len(self.calls) == 1

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "HEAD", "get", "", "TRACE"])
    def test_disallowed_method(self, method):
        with pytest.raises(ValueError):
            self._call(lambda r: httpx.Response(200), method=method)
        assert self.calls == []

    # -- responses ----------------------------------------------------------

    def test_200_body_status_headers(self):
        status, headers, body = self._call(
            lambda r: httpx.Response(
                200, content=b'{"a":1}', headers={"X-Test": "yes"},
            ),
        )
        assert status == 200
        assert body == b'{"a":1}'
        assert isinstance(headers, httpx.Headers)
        assert headers["x-test"] == "yes"

    @pytest.mark.parametrize("code", [400, 401, 404, 422, 500])
    def test_error_status_returned_not_raised(self, code):
        status, _h, body = self._call(
            lambda r: httpx.Response(code, content=b'{"detail":"nope"}'),
        )
        assert status == code
        assert body == b'{"detail":"nope"}'

    @pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
    def test_redirect_blocked_and_not_followed(self, code):
        def handler(request):
            return httpx.Response(code, headers={"Location": "http://169.254.169.254/"})

        with pytest.raises(http_safe.UnsafeURLError, match="redirect"):
            self._call(handler)
        assert len(self.calls) == 1

    # -- size cap -----------------------------------------------------------

    def test_body_exactly_max_bytes_ok(self):
        _s, _h, body = self._call(
            lambda r: httpx.Response(200, content=b"a" * 100), max_bytes=100,
        )
        assert len(body) == 100

    def test_body_over_max_bytes_rejected(self):
        with pytest.raises(http_safe.UnsafeURLError, match="max_bytes=100"):
            self._call(
                lambda r: httpx.Response(200, content=b"a" * 101), max_bytes=100,
            )

    def test_streamed_body_aborted_mid_stream(self):
        consumed = []

        async def gen():
            for _ in range(1000):
                consumed.append(1)
                yield b"a" * 10

        def handler(request):
            return httpx.Response(200, content=gen())

        with pytest.raises(http_safe.UnsafeURLError, match="max_bytes"):
            self._call(handler, max_bytes=50)
        assert len(consumed) < 1000

    # -- request shaping ----------------------------------------------------

    def test_post_json_and_headers(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["body"] = request.content
            seen["ctype"] = request.headers.get("content-type")
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(201, content=b"{}")

        status, _h, _b = self._call(
            handler, method="POST",
            json={"icao": "abc123", "n": 1},
            headers={"Authorization": "Bearer t0k", "X-CSRF": "1"},
        )
        assert status == 201
        assert seen["method"] == "POST"
        import json as _json
        assert _json.loads(seen["body"]) == {"icao": "abc123", "n": 1}
        assert seen["ctype"].startswith("application/json")
        assert seen["auth"] == "Bearer t0k"
        assert self.calls[0].headers["x-csrf"] == "1"

    def test_get_sends_no_body(self):
        self._call(lambda r: httpx.Response(200))
        assert self.calls[0].content == b""

    def test_delete(self):
        status, _h, body = self._call(
            lambda r: httpx.Response(200, content=b"gone"), method="DELETE",
        )
        assert self.calls[0].method == "DELETE"
        assert (status, body) == (200, b"gone")

    # -- client / error propagation ----------------------------------------

    def test_sync_client_rejected(self):
        sync_client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        )
        try:
            with pytest.raises((TypeError, RuntimeError), match="AsyncClient"):
                run_async(http_safe.loopback_request(
                    sync_client, "GET", "http://127.0.0.1/",
                    max_bytes=10, timeout=1.0,
                ))
        finally:
            sync_client.close()

    def test_timeout_propagates(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(httpx.TimeoutException):
            self._call(handler)

    def test_connect_error_propagates(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(httpx.ConnectError):
            self._call(handler)
