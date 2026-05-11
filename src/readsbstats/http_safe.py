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
import urllib.error
import urllib.parse
import urllib.request

try:
    import httpx  # only needed by safe_httpx_get
except ImportError:  # pragma: no cover — httpx is a runtime dep
    httpx = None  # type: ignore[assignment]


_USER_AGENT = {"User-Agent": "readsbstats/1.0"}


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
    """Raise ``ValueError`` if *url* is not safe to fetch."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"non-https URL rejected: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    port = parsed.port or 443
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed for {host!r}: {e}") from e
    for info in infos:
        addr = info[4][0]
        if not _ip_is_public(addr):
            raise ValueError(
                f"URL {url!r} resolves to non-public address {addr!r}"
            )


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
) -> tuple[bytes, object]:
    """HTTPS-only urllib fetch with size cap and SSRF guards.

    Returns ``(body_bytes, headers)`` — headers is the raw
    ``http.client.HTTPMessage`` (supports ``.get(name)`` lookups).

    Raises ``ValueError`` on policy violations (non-https, private IP,
    response too large), ``urllib.error.HTTPError`` on redirects or other
    HTTP errors, and ``OSError`` / ``socket.timeout`` on network failure.
    """
    validate_url(url)
    headers = dict(_USER_AGENT)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with _no_redirect_opener.open(req, timeout=timeout) as resp:
        # If a future change ever permitted redirects, re-check the response URL.
        final = getattr(resp, "url", None)
        if final and final != url:
            validate_url(final)
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(
                f"response from {url} exceeded max_bytes={max_bytes}"
            )
        out_headers = resp.headers
    return data, out_headers


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
