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

These properties match the frontend ``safeHttpUrl()`` policy in
``static/js/table-utils.js``.
"""
from __future__ import annotations

import ipaddress
import socket
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


# ---------------------------------------------------------------------------
# DNS-rebinding TOCTOU guard
# ---------------------------------------------------------------------------
# `validate_url` does a fresh DNS lookup and checks every resolved IP is
# public. The subsequent ``urlopen``/``httpx`` call would normally resolve
# DNS again — a hostile authoritative server can return a public IP first
# (passing our validation) and a private IP on the second lookup (the
# rebinding attack). We close that gap by pinning the validated infos in
# thread-local storage and installing a process-wide ``socket.getaddrinfo``
# wrapper that returns the pinned infos for the same host within the same
# thread.  Other threads, and other hostnames, fall through to the real
# resolver unchanged.

_real_getaddrinfo = socket.getaddrinfo
_dns_pin = threading.local()


def _pinned_getaddrinfo(host, *args, **kwargs):
    pins = getattr(_dns_pin, "pins", None)
    if pins is not None:
        cached = pins.get(host)
        if cached is not None:
            return cached
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _pinned_getaddrinfo  # type: ignore[assignment]


def _set_dns_pin(host: str, infos) -> None:
    pins = getattr(_dns_pin, "pins", None)
    if pins is None:
        pins = {}
        _dns_pin.pins = pins
    pins[host] = infos


def _clear_dns_pin() -> None:
    pins = getattr(_dns_pin, "pins", None)
    if pins is not None:
        pins.clear()


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


def validate_url(url: str) -> None:
    """Raise ``ValueError`` if *url* is not safe to fetch.

    On success, pins the validated DNS answer in thread-local storage so
    the subsequent fetch resolves to the same IPs (closes the rebinding
    TOCTOU described above). Callers should invoke ``_clear_dns_pin()`` in
    a ``finally`` block after the fetch — both :func:`safe_urlopen` and
    :func:`safe_httpx_get` do this for you.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"non-https URL rejected: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    port = parsed.port or 443
    try:
        # Go straight to the real resolver — don't read from our own pin
        # cache or we could re-validate ourselves into a loop.
        infos = _real_getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed for {host!r}: {e}") from e
    for info in infos:
        addr = info[4][0]
        if not _ip_is_public(addr):
            raise ValueError(
                f"URL {url!r} resolves to non-public address {addr!r}"
            )
    _set_dns_pin(host, infos)


# ---------------------------------------------------------------------------
# urllib path
# ---------------------------------------------------------------------------

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Blocks all 3xx redirects.  A redirect is the SSRF vector we care about
    most: a hostile photo-API response can otherwise bounce us to
    http://169.254.169.254/ or another internal endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect blocked (would go to {newurl})",
            headers, fp,
        )


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler())


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
    validate_url(url)
    headers = dict(_USER_AGENT)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with _no_redirect_opener.open(req, timeout=timeout) as resp:
            # If a future change ever permitted redirects, re-check the response URL.
            final = getattr(resp, "url", None)
            if final and final != url:
                validate_url(final)
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError(
                    f"response from {url} exceeded max_bytes={max_bytes}"
                )
            out_headers = resp.headers
    finally:
        # Drop the DNS pin so future calls re-resolve cleanly.
        _clear_dns_pin()
    return body, out_headers


# ---------------------------------------------------------------------------
# httpx path
# ---------------------------------------------------------------------------

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
      * ``follow_redirects=False`` — any 3xx surfaces as ``ValueError``
      * post-call body size check (``len(resp.content) <= max_bytes``)

    Returns the response on success.  Raises ``ValueError`` on policy
    violation; the caller is responsible for ``raise_for_status()`` on the
    returned response if it cares about non-2xx.
    """
    validate_url(url)
    kwargs: dict = {"follow_redirects": False}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
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
    finally:
        # Drop the DNS pin so future calls re-resolve cleanly.
        _clear_dns_pin()
    return resp
