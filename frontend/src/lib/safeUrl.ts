// Originally ported from v1's `static/js/table-utils.js:safeHttpUrl()`
// (since deleted in the v2.0.0 SPA cutover).
//
// This is an HTTPS-only *protocol* guard for URLs we render from third-party
// API data (photo URLs from Planespotters / airport-data.com / hexdb.io /
// Wikipedia) — NOT a host allowlist. Host-allowlisting happens server-side,
// before the URL is ever cached: `photo_sources.py::_check_hosts` (per-source
// CDN allowlist) and the SSRF IP-gating in `http_safe.py`. By the time a URL
// reaches the SPA it has already passed those checks; this is the last-line
// render-time defence.
//
// React's JSX escapes text, but `<img src>` / `<a href>` are still vectors:
// `javascript:`, `data:`, `vbscript:`, `file:`, and protocol-relative URIs
// can all execute or exfiltrate without further sanitisation.
//
// All upstream photo sources we use serve HTTPS, so HTTPS-only is the right
// policy. http:// is rejected — if a future source ships HTTP-only, that's
// a deliberate decision we should revisit, not a silent allow.
//
// Returns the trimmed URL on success, or '' on rejection. The empty-string
// return is convenient for JSX (`<img src={safeUrl(...)}>` won't render).
export function safeUrl(input: string | null | undefined): string {
  if (!input) return '';
  const trimmed = input.trim();
  if (!trimmed) return '';
  try {
    const url = new URL(trimmed);
    if (url.protocol !== 'https:') return '';
    return trimmed;
  } catch {
    return '';
  }
}
