"""Shared SSRF-safe HTTP helpers.

All outbound HTTP from readsbstats goes through one of:

  * :func:`safe_urlopen` for ``urllib.request``-style callers (photo sources,
    db_updater).
  * :func:`safe_httpx_get` for ``httpx``-style callers (route_enricher,
    adsbx_enricher).

Both helpers enforce:

  * HTTPS only.
  * Hostname must resolve to a public IP (``ipaddress`` checks against
    private / loopback / link-local / reserved / multicast / unspecified).
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
attack). Audit 12 closed this at the protocol level:

  * **urllib path** uses a custom :class:`_PinnedHTTPSConnection` that
    connects to the pre-validated IP directly. No DNS lookup happens
    between ``validate_url`` and the connect, so there is no second
    lookup to rebind. SNI / cert validation use the original hostname.

  * **httpx path** wraps the call in :func:`_pinned_socket_resolver`, a
    context-manager that temporarily redirects ``socket.getaddrinfo``
    to return the pre-validated info tuple for the duration of the
    single request (and only for the host we care about). The
    redirection is undone in ``finally``; no module-load global patch.
    Other ``socket.getaddrinfo`` callers in the process (uvicorn,
    background threads, tests) are unaffected outside the brief
    redirection window.

Async httpx is rejected with a clear error (see :func:`safe_httpx_get`)
because the scoped redirection above uses ``socket.getaddrinfo`` which
async httpx bypasses via ``anyio.getaddrinfo``. We don't currently use
async httpx anywhere; the guard is defensive against future drift.
"""
from __future__ import annotations

import contextlib
import http.client
import ipaddress
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

try:
    import httpx  # only needed by safe_httpx_get
except ImportError:  # pragma: no cover — httpx is a runtime dep
    httpx = None  # type: ignore[assignment]


_USER_AGENT = {"User-Agent": "readsbstats/1.0"}


class TransientError(Exception):
    """Raised by callers on retry-able failures (network timeout, 5xx,
    file-read errors). The caller's outer loop catches this and applies
    backoff before retrying.

    Audit-12 #198 — three modules (route_enricher, adsbx_enricher,
    metrics_collector) used to declare their own identical
    `_TransientError` classes. They now alias this single definition
    via `_TransientError = http_safe.TransientError`.
    """


# Captured at import so test monkey-patches of ``socket.getaddrinfo``
# can't accidentally feed the validator faked data. Tests patch this
# name directly when they want to inject resolution results.
_real_getaddrinfo = socket.getaddrinfo


# ---------------------------------------------------------------------------
# URL / IP validation
# ---------------------------------------------------------------------------

def _ip_is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_and_validate(
    url: str,
) -> tuple[urllib.parse.ParseResult, list]:
    """Return ``(parsed_url, addrinfo_list)`` for *url*. Raises ``ValueError``
    if the URL violates policy (non-HTTPS, no host, any resolved IP is
    non-public, or DNS resolution fails).

    Used by both the urllib and httpx code paths so the validation +
    resolution happens exactly once per request.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"non-https URL rejected: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    port = parsed.port or 443
    try:
        infos = _real_getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed for {host!r}: {e}") from e
    for info in infos:
        addr = info[4][0]
        if not _ip_is_public(addr):
            raise ValueError(
                f"URL {url!r} resolves to non-public address {addr!r}"
            )
    return parsed, infos


def validate_url(url: str) -> None:
    """Raise ``ValueError`` if *url* is not safe to fetch.

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
# httpx path — scoped socket.getaddrinfo redirection
# ---------------------------------------------------------------------------

_RESOLVER_LOCK = threading.Lock()


@contextlib.contextmanager
def _pinned_socket_resolver(hostname: str, infos):
    """Temporarily redirect ``socket.getaddrinfo`` so that lookups for
    *hostname* return *infos* (and only this hostname is affected; other
    lookups fall through to the real resolver).

    Used by :func:`safe_httpx_get` to close the rebinding TOCTOU on the
    httpx path. Restores ``socket.getaddrinfo`` in ``finally`` — no
    module-load global patch.

    Audit-13 A13-015: protected by a module-level lock so two concurrent
    httpx requests can't nest each other's patch — previously a nested
    capture left the inner resolver stuck as the module-level
    ``socket.getaddrinfo`` after the outer ``finally`` ran. The lock
    serializes concurrent pins; urllib is unaffected (it uses
    ``_PinnedHTTPSConnection``, not the global resolver).
    """
    with _RESOLVER_LOCK:
        original = socket.getaddrinfo

        def _resolver(host, *args, **kwargs):
            if host == hostname:
                return infos
            return original(host, *args, **kwargs)

        socket.getaddrinfo = _resolver  # type: ignore[assignment]
        try:
            yield
        finally:
            socket.getaddrinfo = original  # type: ignore[assignment]


_REDIRECT_CODES = {301, 302, 303, 307, 308}


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
      * the validated IP is pinned for the duration of the call via a
        scoped ``socket.getaddrinfo`` redirection
      * ``follow_redirects=False`` — any 3xx surfaces as ``ValueError``
      * post-call body size check (``len(resp.content) <= max_bytes``)

    Async clients are rejected (audit-12 H2 guard): the scoped pin uses
    ``socket.getaddrinfo`` which ``httpx.AsyncClient`` bypasses via
    ``anyio.getaddrinfo``. We don't use async httpx anywhere today;
    surface the limitation loudly so a future use doesn't silently
    bypass the rebinding guard.

    Returns the response on success.  Raises ``ValueError`` on policy
    violation; the caller is responsible for ``raise_for_status()`` on the
    returned response if it cares about non-2xx.
    """
    if httpx is not None and isinstance(client, httpx.AsyncClient):
        raise RuntimeError(
            "safe_httpx_get does not support httpx.AsyncClient — "
            "async DNS resolution bypasses our rebinding guard."
        )

    parsed, infos = _resolve_and_validate(url)
    hostname = parsed.hostname  # already validated non-empty above
    assert hostname is not None  # for the type checker

    kwargs: dict = {"follow_redirects": False}
    if timeout is not None:
        kwargs["timeout"] = timeout

    # Audit-13 A13-016: stream the body so we can abort once `max_bytes`
    # is exceeded, instead of buffering the entire payload before the
    # size check. A hostile upstream returning a multi-GB body used to
    # be fully loaded into RAM before the post-call `len(resp.content)`
    # check rejected it — OOM exposure on the Pi. Test fakes without a
    # `.stream()` method fall back to `.get()` + post-check.
    can_stream = hasattr(client, "stream") and callable(getattr(client, "stream", None))
    with _pinned_socket_resolver(hostname, infos):
        if can_stream:
            with client.stream("GET", url, **kwargs) as resp:
                if resp.status_code in _REDIRECT_CODES:
                    raise ValueError(
                        f"redirect blocked: GET {url} -> {resp.status_code} "
                        f"-> {resp.headers.get('Location', '?')!r}"
                    )
                buf = bytearray()
                for chunk in resp.iter_bytes(8192):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise ValueError(
                            f"response from {url} exceeded max_bytes={max_bytes} "
                            f"(streamed {len(buf)} before cutoff)"
                        )
                # iter_bytes consumed the stream; patch the body in for
                # callers that read .content or .json() afterwards.
                resp._content = bytes(buf)  # type: ignore[attr-defined]
            return resp
        # Compatibility path for test fakes that only mock .get().
        resp = client.get(url, **kwargs)
        if resp.status_code in _REDIRECT_CODES:
            raise ValueError(
                f"redirect blocked: GET {url} -> {resp.status_code} "
                f"-> {resp.headers.get('Location', '?')!r}"
            )
        if len(resp.content) > max_bytes:
            raise ValueError(
                f"response from {url} exceeded max_bytes={max_bytes} "
                f"(got {len(resp.content)})"
            )
        return resp
