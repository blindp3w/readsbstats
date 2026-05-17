# Centralised SSRF guard via http_safe.py

- Status: ACCEPTED
- Date: 2026-05-11

## Context

The application makes outbound HTTP calls to several third-party APIs (Planespotters, airport-data.com, hexdb.io, Wikipedia, adsbdb.com, airplanes.live, Telegram, GitHub raw). Without a central policy, individual call sites could differ in redirect handling, TLS enforcement, and response size limits — creating SSRF risk and inconsistent behaviour.

## Decision

All outbound HTTP calls go through one of two helpers in `http_safe.py`:
- `safe_urlopen()` for urllib-based calls
- `safe_httpx_get()` for httpx-based calls

Both enforce:
1. HTTPS-only (reject `http://`)
2. Public-IP-only — reject any host resolving to RFC1918 / loopback / link-local / metadata (169.254.169.254) / reserved / multicast / unspecified addresses
3. No redirect following (`_NoRedirectHandler` for urllib; `follow_redirects=False` for httpx)
4. Per-call `max_bytes` cap

## Consequences

- Any new outbound call must use the appropriate helper and set a `max_bytes` sized for the expected payload (a few KB for JSON APIs, up to tens of MB for binary downloads).
- If an upstream legitimately redirects (e.g., GitHub `/raw/` → `raw.githubusercontent.com`), use the direct final URL instead — chasing redirects safely would require post-flight IP re-validation that the helper deliberately avoids.
- A regression test asserts `_NoRedirectHandler` is wired in the urllib opener.
