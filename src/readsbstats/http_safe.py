"""Shared SSRF-safe HTTP helpers.

All outbound HTTP from readsbstats goes through one of:

  * :func:`safe_urlopen` for ``urllib.request``-style callers (photo sources,
    db_updater).
  * :func:`safe_httpx_get` for ``httpx``-style callers (route_enricher,
    adsbx_enricher).

Both helpers enforce:

  * HTTPS only.
  * Hostname must resolve to a globally-reachable IP
    (``ipaddress.ip_address(x).is_global``). This rejects CGNAT shared
    address space (100.64/10), benchmark space (198.18/15), and every
    other non-global category in one predicate — previously the policy
    was a list of exclusions that missed CGNAT (PY-1, audit 2026-05-31).
  * Redirects are not followed — a 3xx upstream response surfaces as an
    error.  This prevents a hostile or compromised upstream from bouncing us
    onto an internal endpoint such as the cloud metadata service.
  * Response body is capped at ``max_bytes``.

These properties match the frontend ``safeUrl()`` policy in
``frontend/src/lib/safeUrl.ts``.

DNS-rebinding TOCTOU
====================

A hostile authoritative DNS server can return a public IP first (passing
``validate_url``) and a private IP on the second lookup (the rebinding
attack). Both code paths close this at the protocol level:

  * **urllib path** uses a custom :class:`_PinnedHTTPSConnection` that
    connects to the pre-validated IP directly. No DNS lookup happens
    between ``validate_url`` and the connect, so there is no second
    lookup to rebind. SNI / cert validation use the original hostname.

  * **httpx path** (audit 2026-05-25 rewrite) rebuilds the request URL
    against the pre-validated IP and uses the httpx
    ``extensions={"sni_hostname": hostname}`` request extension to keep
    SNI + cert validation pinned to the original hostname. A ``Host``
    header override preserves the original host:port for virtual-host
    routing on the upstream. There is no ``socket.getaddrinfo``
    mutation — concurrent calls from different threads / hosts no
    longer serialize. Previously this path patched the process-wide
    resolver under a mutex (`Audit-13 A13-015`); see git history for the
    rationale and the bug that prompted the rewrite.

Async httpx is rejected with a clear error (see :func:`safe_httpx_get`)
because no production path uses it today and reviewing the async-only
edge cases (cancel + transport teardown) is out of scope for the SSRF
funnel.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from types import MappingProxyType

try:
    import httpx  # only needed by safe_httpx_get
except ImportError:  # pragma: no cover — httpx is a runtime dep
    httpx = None  # type: ignore[assignment]


# Audit-13 A13-053: read-only view so downstream re-exports (notably
# photo_sources.PHOTO_UA) can't be `.pop()`/`.update()`'d by accident
# and silently corrupt headers for every subsequent fetch. `dict(...)`
# at the use sites already copies; the immutable wrapper enforces it.
_USER_AGENT = MappingProxyType({"User-Agent": "readsbstats/1.0"})


class TransientError(Exception):
    """Raised by callers on retry-able failures (network timeout, 5xx,
    file-read errors). The caller's outer loop catches this and applies
    backoff before retrying.

    Audit-12 #198 — three modules (route_enricher, adsbx_enricher,
    metrics_collector) used to declare their own identical
    `_TransientError` classes. They now alias this single definition
    via `_TransientError = http_safe.TransientError`.
    """


class UnsafeURLError(ValueError):
    """Raised when a URL or response violates a security policy.

    Distinct from DNS/network failures so callers can classify this as a
    permanent error (retrying the same URL will hit the same rejection).
    Examples: non-HTTPS scheme, destination IP not public, redirect
    detected, response body exceeded max_bytes.

    DNS resolution failures are plain ``ValueError`` — they are transient
    and should not be treated as permanent.
    """


# Captured at import so test monkey-patches of ``socket.getaddrinfo``
# can't accidentally feed the validator faked data. Tests patch this
# name directly when they want to inject resolution results.
_real_getaddrinfo = socket.getaddrinfo


# SEC-1 (audit 18): IPv6-transition prefixes that embed or tunnel to
# private space. ipaddress.is_global reports these as global (correct per
# IANA), but each can carry traffic to an internal v4 destination such as
# the cloud metadata service — the 2026 NAT64 / 6to4 / Teredo / site-local
# SSRF-bypass class. We blanket-reject the whole prefixes (no embedded-v4
# re-extraction needed) and do NOT rely on is_global alone, whose
# classification shifts across Python patch versions.
#   64:ff9b::/96   — well-known NAT64 (RFC 6052)
#   64:ff9b:1::/48 — local-use NAT64 (RFC 8215)
#   2002::/16      — 6to4 (RFC 3056)
#   2001::/32      — Teredo (RFC 4380)
#   fec0::/10      — deprecated site-local (RFC 3879)
_SSRF_DENY_NETS = [
    ipaddress.ip_network(n)
    for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16",
              "2001::/32", "fec0::/10")
]


# ---------------------------------------------------------------------------
# URL / IP validation
# ---------------------------------------------------------------------------

def _ip_is_public(addr: str) -> bool:
    # PY-1 (Audit 2026-05-31): require is_global so CGNAT (100.64/10) is
    # rejected — the previous exclusion-list approach missed it because
    # ipaddress doesn't mark CGNAT as private/loopback/reserved/etc.
    # We additionally exclude multicast because Python's is_global is True
    # for multicast (both v4 224/4 and v6 ffXX::) — that's correct per
    # IANA but wrong for a unicast-only egress policy: multicast must
    # never be the destination of an outbound HTTPS request.
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    # SEC-1 (audit 18): reject IPv6-transition prefixes outright. These
    # are is_global=True but route/tunnel to private space (NAT64, 6to4,
    # Teredo, site-local) — a v6 SSRF bypass onto internal v4 endpoints.
    if ip.version == 6 and any(ip in net for net in _SSRF_DENY_NETS):
        return False
    return ip.is_global and not ip.is_multicast


def _resolve_and_validate(
    url: str,
) -> tuple[urllib.parse.ParseResult, list]:
    """Return ``(parsed_url, addrinfo_list)`` for *url*.

    Raises ``UnsafeURLError`` (a ``ValueError`` subclass) for policy
    violations: non-HTTPS scheme, missing host, or a resolved IP that is
    not globally routable.

    Raises plain ``ValueError`` for DNS resolution failures — these are
    transient and callers should treat them differently from permanent
    policy rejections.

    Used by both the urllib and httpx code paths so validation + resolution
    happens exactly once per request.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeURLError(f"non-https URL rejected: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    # SEC-3 (audit 18): reject embedded credentials. A
    # `https://user:pass@host/` URL is a policy violation — it leaks the
    # userinfo into the Host header on the httpx path and is a host-
    # confusion / phishing vector. Treat it like any other rejection.
    if parsed.username or parsed.password:
        raise UnsafeURLError(
            f"URL with embedded credentials rejected: host {host!r}"
        )
    port = parsed.port or 443
    try:
        infos = _real_getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed for {host!r}: {e}") from e
    for info in infos:
        addr = info[4][0]
        if not _ip_is_public(addr):
            raise UnsafeURLError(
                f"URL {url!r} resolves to non-public address {addr!r}"
            )
    return parsed, infos


def validate_url(url: str) -> None:
    """Raise ``UnsafeURLError`` (a ``ValueError`` subclass) for policy
    violations (non-HTTPS, private/loopback destination IP), or plain
    ``ValueError`` for DNS resolution failures.

    Performs the same DNS + IP-publicness check as
    :func:`_resolve_and_validate` but discards the resolution result.
    Kept for back-compat with callers that want to validate without
    fetching (e.g. config-time checks).
    """
    _resolve_and_validate(url)


# ---------------------------------------------------------------------------
# urllib path — custom HTTPSConnection that connects to the pre-validated IP
# ---------------------------------------------------------------------------

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An ``http.client.HTTPSConnection`` that:

      * Connects to a specific IP (passed in at construction), not the
        URL's host. No second DNS lookup happens — the rebinding TOCTOU
        is closed at the protocol layer.
      * Uses the original hostname for TLS SNI and certificate hostname
        verification (``ssl_context.wrap_socket(server_hostname=...)``).
      * Sets the ``Host:`` HTTP header to the original hostname (urllib
        does this automatically when ``self.host`` matches the request URL).

    Audit 12 H1/H2 — replaces the previous "globally patch
    socket.getaddrinfo with a thread-local pin" design. That worked but
    was brittle to test (any ``monkeypatch.setattr(socket, "getaddrinfo")``
    was silently no-op'd) and didn't naturally cover async clients.
    """

    def __init__(self, *, hostname: str, target_ip: str, port: int,
                 timeout: float, context: ssl.SSLContext | None = None):
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self._target_ip = target_ip
        # `self.host` (used for the Host header) stays at the hostname;
        # we only override the TCP target in `connect()`.

    def connect(self) -> None:
        # Connect to the pinned IP rather than re-resolving self.host.
        sock = socket.create_connection(
            (self._target_ip, self.port),
            timeout=self.timeout,
        )
        # TLS handshake — server_hostname=self.host gives us correct SNI
        # AND triggers Python's standard hostname verification against
        # the cert's SubjectAltName.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """``urllib.request.HTTPSHandler`` that issues every request through a
    :class:`_PinnedHTTPSConnection`. Built fresh per call so a stale pinned
    IP can't leak across requests."""

    def __init__(self, hostname: str, target_ip: str, port: int,
                 timeout: float, context: ssl.SSLContext):
        super().__init__(context=context)
        self._hostname = hostname
        self._target_ip = target_ip
        self._port = port
        self._timeout = timeout
        self._context = context

    def https_open(self, req):
        # The `do_open` helper instantiates the connection class with
        # (host, timeout, context) and then issues the request. Our
        # connection class ignores the `host` arg and uses the pre-pinned
        # IP — but we still pass the original hostname so urllib's Host
        # header generation is correct.
        return self.do_open(self._make_connection, req)

    def _make_connection(self, host, timeout=None, context=None,
                         **_kwargs):
        # Note: urllib passes `host` as the URL's host (which we want for
        # the Host header) plus `timeout` and `context` from the request.
        # We honour the timeout arg if present, else fall back to our
        # constructor default.
        return _PinnedHTTPSConnection(
            hostname=host,
            target_ip=self._target_ip,
            port=self._port,
            timeout=timeout if timeout is not None else self._timeout,
            context=self._context,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Blocks all 3xx redirects.  A redirect is the SSRF vector we care about
    most: a hostile photo-API response can otherwise bounce us to
    http://169.254.169.254/ or another internal endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect blocked (would go to {newurl})",
            headers, fp,
        )


def _build_pinned_opener(parsed: urllib.parse.ParseResult, target_ip: str,
                        timeout: float) -> urllib.request.OpenerDirector:
    """Build a one-shot opener wired to a single ``_PinnedHTTPSConnection``
    plus the no-redirect handler."""
    context = ssl.create_default_context()
    handler = _PinnedHTTPSHandler(
        hostname=parsed.hostname,
        target_ip=target_ip,
        port=parsed.port or 443,
        timeout=timeout,
        context=context,
    )
    return urllib.request.build_opener(handler, _NoRedirectHandler())


def safe_urlopen(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    extra_headers: dict | None = None,
    data: bytes | None = None,
) -> tuple[bytes, object]:
    """HTTPS-only urllib fetch with size cap and SSRF guards.

    Returns ``(body_bytes, headers)`` — headers is the raw
    ``http.client.HTTPMessage`` (supports ``.get(name)`` lookups).

    When ``data`` is supplied the request is a POST with that body; the
    caller is responsible for setting any required Content-Type via
    ``extra_headers``.  All other policies (HTTPS-only, public-IP-only,
    no-redirect, size cap) apply to POSTs identically.

    Raises ``ValueError`` on policy violations (non-https, private IP,
    response too large), ``urllib.error.HTTPError`` on redirects or other
    HTTP errors, and ``OSError`` / ``socket.timeout`` on network failure.
    """
    parsed, infos = _resolve_and_validate(url)
    # Pick the first IP that getaddrinfo returned. All entries passed the
    # public-IP check (else `_resolve_and_validate` would have raised), so
    # any choice is safe.
    target_ip = infos[0][4][0]

    opener = _build_pinned_opener(parsed, target_ip, timeout)

    headers = dict(_USER_AGENT)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        final = getattr(resp, "url", None)
        if final and final != url:
            # If a future change ever permitted redirects, re-validate
            # the final URL. Today _NoRedirectHandler rejects 3xx so
            # this is unreachable; keep it as defence in depth.
            _resolve_and_validate(final)
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(
                f"response from {url} exceeded max_bytes={max_bytes}"
            )
        out_headers = resp.headers
    return body, out_headers


# ---------------------------------------------------------------------------
# httpx path — direct-IP URL + sni_hostname extension
# ---------------------------------------------------------------------------

_REDIRECT_CODES = {301, 302, 303, 307, 308}


def _build_pinned_httpx_request(url: str) -> tuple[str, dict, dict]:
    """Resolve+validate *url* and return the per-request ``(pinned_url,
    headers, extensions)`` triple that connects to the validated IP while
    keeping TLS/SNI/Host pinned to the original hostname.

    Audit 2026-05-25: this replaces the previous ``socket.getaddrinfo``
    monkey-patch. There is no process-wide mutation, so concurrent httpx
    requests no longer serialize through ``_RESOLVER_LOCK``.
    """
    parsed, infos = _resolve_and_validate(url)
    hostname = parsed.hostname  # validated non-empty above
    assert hostname is not None  # for the type checker
    target_ip = infos[0][4][0]

    # Bracket IPv6 literals so they're a valid URL netloc.
    netloc_ip = f"[{target_ip}]" if ":" in target_ip else target_ip
    if parsed.port:
        netloc_ip = f"{netloc_ip}:{parsed.port}"

    pinned_url = urllib.parse.urlunparse((
        parsed.scheme,
        netloc_ip,
        parsed.path or "/",
        parsed.params,
        parsed.query,
        "",  # drop fragment — never sent over the wire anyway
    ))

    # Host header keeps the original host[:port] so virtual-host upstreams
    # (CloudFront, shared backends) still route the request to the right
    # site, and any Host-aware request signing on the upstream still works.
    # SEC-3 (audit 18): build it from hostname[:port], NOT parsed.netloc —
    # netloc carries any `user:pass@` userinfo, which must never leak into
    # the Host header. (Credentialed URLs are also rejected upstream in
    # _resolve_and_validate; this is defence in depth.)
    # Bracket an IPv6 literal so the Host header stays well-formed — parsed.hostname
    # strips the brackets that parsed.netloc kept (code-review follow-up to SEC-3).
    host_for_header = f"[{hostname}]" if ":" in hostname else hostname
    host_header = host_for_header + (f":{parsed.port}" if parsed.port else "")
    headers = {"Host": host_header}
    # sni_hostname overrides the SNI extension AND the cert hostname
    # verification target in httpx 0.24+.
    extensions = {"sni_hostname": hostname}
    return pinned_url, headers, extensions


def safe_httpx_get(
    client: "httpx.Client",
    url: str,
    *,
    max_bytes: int,
    timeout: float | None = None,
) -> "httpx.Response":
    """HTTPS-only httpx GET with size cap and no-redirect policy.

    The caller passes its own ``httpx.Client`` so connection pooling /
    user-agent configuration remains the caller's concern.  This helper
    enforces:
      * pre-call URL validation (HTTPS + public IP)
      * the validated IP is connected to directly by rewriting the URL
        netloc; SNI + cert validation stay pinned to the original
        hostname via the ``sni_hostname`` httpx request extension
      * ``follow_redirects=False`` — any 3xx surfaces as ``ValueError``
      * post-call body size check (``len(resp.content) <= max_bytes``)

    Async clients are rejected: no production path uses them today and
    routing async cancel + transport teardown through this funnel is out
    of scope.

    Returns the response on success.  Raises ``UnsafeURLError`` (a
    ``ValueError`` subclass) on policy violations (non-HTTPS, private IP,
    redirect detected, body over max_bytes).  The caller is responsible
    for ``raise_for_status()`` on the returned response if it cares about
    non-2xx status codes.
    """
    if httpx is not None and isinstance(client, httpx.AsyncClient):
        raise RuntimeError(
            "safe_httpx_get does not support httpx.AsyncClient — "
            "async paths are not routed through this SSRF funnel."
        )

    pinned_url, pin_headers, extensions = _build_pinned_httpx_request(url)

    kwargs: dict = {
        "follow_redirects": False,
        "headers": pin_headers,
        "extensions": extensions,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout

    # Audit-13 A13-016: stream the body so we can abort once `max_bytes`
    # is exceeded, instead of buffering the entire payload before the
    # size check. A hostile upstream returning a multi-GB body used to
    # be fully loaded into RAM before the post-call `len(resp.content)`
    # check rejected it — OOM exposure on the Pi. Test fakes without a
    # `.stream()` method fall back to `.get()` + post-check.
    can_stream = hasattr(client, "stream") and callable(getattr(client, "stream", None))
    if can_stream:
        with client.stream("GET", pinned_url, **kwargs) as resp:
            if resp.status_code in _REDIRECT_CODES:
                raise UnsafeURLError(
                    f"redirect blocked: GET {url} -> {resp.status_code} "
                    f"-> {resp.headers.get('Location', '?')!r}"
                )
            buf = bytearray()
            for chunk in resp.iter_bytes(8192):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise UnsafeURLError(
                        f"response from {url} exceeded max_bytes={max_bytes} "
                        f"(streamed {len(buf)} before cutoff)"
                    )
            # iter_bytes consumed the stream; patch the body in for
            # callers that read .content or .json() afterwards.
            resp._content = bytes(buf)  # type: ignore[attr-defined]
        return resp
    # Compatibility path for test fakes that only mock .get().
    resp = client.get(pinned_url, **kwargs)
    if resp.status_code in _REDIRECT_CODES:
        raise UnsafeURLError(
            f"redirect blocked: GET {url} -> {resp.status_code} "
            f"-> {resp.headers.get('Location', '?')!r}"
        )
    if len(resp.content) > max_bytes:
        raise UnsafeURLError(
            f"response from {url} exceeded max_bytes={max_bytes} "
            f"(got {len(resp.content)})"
        )
    return resp
