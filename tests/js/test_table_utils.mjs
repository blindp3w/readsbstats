/* Unit tests for static/js/table-utils.js — pure helpers only.
   `flagBadge` is the only DOM-free function; the rest (initSortHeaders,
   renderPagination, loadPhoto) need a real document/fetch and aren't
   sensibly testable without a DOM emulator. */

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadJs } from "./_loader.mjs";

function loadTableUtils() {
  // table-utils.js references `document`, `ROOT`, `escHtml`, `fetch`, `URL` at
  // top-level only inside function bodies; loading the source itself only
  // needs the `document` reference for `document.querySelectorAll(...)` to
  // exist as a name (it's never invoked during module-load).
  return loadJs("static/js/table-utils.js", {
    document: {
      querySelectorAll: () => [],
      getElementById: () => null,
      createElement: () => ({}),
    },
    ROOT: "",
    escHtml: (s) => String(s),
    fetch: async () => ({ ok: false, status: 404 }),
    URL,
  });
}

// ---------------------------------------------------------------------------
// flagBadge — bitmask interpretation
//
// Note: FLAG_MILITARY/FLAG_INTERESTING are declared with `const` in the source,
// so they don't attach to the VM global object. The constants are still tested
// indirectly via the bitmask cases below.
// ---------------------------------------------------------------------------

test("flagBadge: military takes precedence over interesting", () => {
  const { flagBadge } = loadTableUtils();
  // 3 = military (1) | interesting (2) — military wins
  const html = flagBadge(3, "short");
  assert.match(html, /badge-mil/);
  assert.equal(html.includes("badge-int"), false);
});

test("flagBadge: military short = MIL", () => {
  const { flagBadge } = loadTableUtils();
  assert.match(flagBadge(1, "short"), />MIL</);
});

test("flagBadge: military long = 'Military aircraft'", () => {
  const { flagBadge } = loadTableUtils();
  assert.match(flagBadge(1, "long"), />Military aircraft</);
});

test("flagBadge: military default style label is 'Military' (no 'aircraft' suffix)", () => {
  const { flagBadge } = loadTableUtils();
  // The label sits between '>' and '</span>'. The title attribute always
  // mentions 'aircraft', so we have to scope the assertion to the label only.
  const html = flagBadge(1, "");
  const labelMatch = html.match(/>([^<]+)<\/span>/);
  assert.ok(labelMatch, "expected a span label");
  assert.equal(labelMatch[1], "Military");
});

test("flagBadge: interesting short = star char", () => {
  const { flagBadge } = loadTableUtils();
  // Star = U+2605
  assert.match(flagBadge(2, "short"), />\u2605</);
});

test("flagBadge: interesting long = 'Interesting aircraft'", () => {
  const { flagBadge } = loadTableUtils();
  assert.match(flagBadge(2, "long"), />Interesting aircraft</);
});

test("flagBadge: no flags returns empty string", () => {
  const { flagBadge } = loadTableUtils();
  assert.equal(flagBadge(0, "short"), "");
});

test("flagBadge: PIA flag (4) alone returns empty (no military, no interesting)", () => {
  const { flagBadge } = loadTableUtils();
  assert.equal(flagBadge(4, "short"), "");
});

test("flagBadge: LADD flag (8) alone returns empty", () => {
  const { flagBadge } = loadTableUtils();
  assert.equal(flagBadge(8, "short"), "");
});

test("flagBadge: military bit set within larger bitmask (mil + PIA + LADD)", () => {
  const { flagBadge } = loadTableUtils();
  // 1 | 4 | 8 = 13 — military bit is set
  assert.match(flagBadge(13, "short"), />MIL</);
});

test("flagBadge: interesting bit set within larger bitmask (no military)", () => {
  const { flagBadge } = loadTableUtils();
  // 2 | 4 | 8 = 14 — interesting set, military not
  assert.match(flagBadge(14, "short"), /\u2605/);
});

// ---------------------------------------------------------------------------
// FLAG_ANONYMOUS = 16 \u2014 non-ICAO Mode-S hex (computed at query time, not stored).
// Precedence: military > interesting > anonymous, so the badge falls through
// in that order even when multiple bits are set.
// ---------------------------------------------------------------------------

test("flagBadge: anonymous short = '?'", () => {
  const { flagBadge } = loadTableUtils();
  assert.match(flagBadge(16, "short"), />\?</);
});

test("flagBadge: anonymous default label = 'Anonymous'", () => {
  const { flagBadge } = loadTableUtils();
  const html = flagBadge(16, "");
  const labelMatch = html.match(/>([^<]+)<\/span>/);
  assert.ok(labelMatch);
  assert.equal(labelMatch[1], "Anonymous");
});

test("flagBadge: anonymous long = 'Anonymous hex'", () => {
  const { flagBadge } = loadTableUtils();
  assert.match(flagBadge(16, "long"), />Anonymous hex</);
});

test("flagBadge: anonymous uses badge-anon CSS class", () => {
  const { flagBadge } = loadTableUtils();
  assert.match(flagBadge(16, "short"), /badge-anon/);
});

test("flagBadge: military + anonymous wins (precedence)", () => {
  // 1 | 16 = 17 \u2014 military takes precedence; the anon bit still rides along
  // in the bitmask for any future double-badge renderer.
  const { flagBadge } = loadTableUtils();
  const html = flagBadge(17, "short");
  assert.match(html, /badge-mil/);
  assert.equal(html.includes("badge-anon"), false);
});

test("flagBadge: interesting + anonymous wins (precedence)", () => {
  const { flagBadge } = loadTableUtils();
  const html = flagBadge(18, "short");
  assert.match(html, /badge-int/);
  assert.equal(html.includes("badge-anon"), false);
});

test("flagBadge: anonymous with PIA/LADD-only bits still surfaces", () => {
  // bits 4 (PIA) and 8 (LADD) have no badge of their own; the anon bit
  // takes precedence when no military/interesting bit is set.
  const { flagBadge } = loadTableUtils();
  assert.match(flagBadge(28, "short"), /badge-anon/); // 16 | 4 | 8
});

// ---------------------------------------------------------------------------
// safeHttpUrl — URL scheme allowlist (XSS protection for href/src)
// ---------------------------------------------------------------------------

test("safeHttpUrl: rejects http:// URLs (https-only allowlist)", () => {
  // Photo providers all serve over HTTPS; rejecting plain http:// closes the
  // MITM window for users on hostile networks.
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("http://example.com/foo"), "");
});

test("safeHttpUrl: accepts https:// URLs", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("https://example.com/foo"), "https://example.com/foo");
});

test("safeHttpUrl: accepts mixed-case https schemes", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("HTTPS://example.com/"), "HTTPS://example.com/");
});

test("safeHttpUrl: returns trimmed URL when leading whitespace is present", () => {
  // Browsers tolerate leading whitespace in href/src but it's inconsistent —
  // return a clean trimmed URL so callers don't propagate the surprise.
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("  https://example.com/x  "), "https://example.com/x");
});

test("safeHttpUrl: rejects javascript: URI", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("javascript:alert(1)"), "");
});

test("safeHttpUrl: rejects javascript: with whitespace prefix and mixed case", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("  JaVaScRiPt:alert(1)"), "");
});

test("safeHttpUrl: rejects data: URI", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("data:text/html,<script>alert(1)</script>"), "");
});

test("safeHttpUrl: rejects vbscript: URI", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("vbscript:msgbox(1)"), "");
});

test("safeHttpUrl: rejects file: URI", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("file:///etc/passwd"), "");
});

test("safeHttpUrl: rejects null/undefined/empty", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl(null), "");
  assert.equal(safeHttpUrl(undefined), "");
  assert.equal(safeHttpUrl(""), "");
});

test("safeHttpUrl: rejects protocol-relative URLs (//evil.com)", () => {
  // Protocol-relative is HTTP/HTTPS in browsers, but we want explicit schemes
  // to keep the allowlist simple and unambiguous.
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("//evil.com/x"), "");
});

// ---------------------------------------------------------------------------
// photoSourceSuffix — derive credit-line " via <source>" from link host
// ---------------------------------------------------------------------------

test("photoSourceSuffix: empty/null returns ''", () => {
  const { photoSourceSuffix } = loadTableUtils();
  assert.equal(photoSourceSuffix(""), "");
  assert.equal(photoSourceSuffix(null), "");
  assert.equal(photoSourceSuffix(undefined), "");
});

test("photoSourceSuffix: planespotters link → ' via Planespotters.net'", () => {
  const { photoSourceSuffix } = loadTableUtils();
  assert.equal(
    photoSourceSuffix("https://www.planespotters.net/photo/123"),
    " via Planespotters.net",
  );
  assert.equal(
    photoSourceSuffix("https://planespotters.net/photo/123"),
    " via Planespotters.net",
  );
});

test("photoSourceSuffix: airport-data link → ' via airport-data.com'", () => {
  const { photoSourceSuffix } = loadTableUtils();
  assert.equal(
    photoSourceSuffix("https://www.airport-data.com/aircraft/N123.html"),
    " via airport-data.com",
  );
});

test("photoSourceSuffix: hexdb link → ' via hexdb.io'", () => {
  const { photoSourceSuffix } = loadTableUtils();
  assert.equal(
    photoSourceSuffix("https://hexdb.io/aircraft/aabbcc"),
    " via hexdb.io",
  );
});

test("photoSourceSuffix: en.wikipedia.org link → ' via Wikipedia'", () => {
  const { photoSourceSuffix } = loadTableUtils();
  assert.equal(
    photoSourceSuffix("https://en.wikipedia.org/wiki/Cessna_152"),
    " via Wikipedia",
  );
});

test("photoSourceSuffix: unknown host returns ''", () => {
  const { photoSourceSuffix } = loadTableUtils();
  assert.equal(
    photoSourceSuffix("https://someotherhost.example/photo"),
    "",
  );
});

test("photoSourceSuffix: malformed URL returns '' (try/catch)", () => {
  const { photoSourceSuffix } = loadTableUtils();
  assert.equal(photoSourceSuffix("not a url"), "");
});

test("photoSourceSuffix: case-insensitive hostname matching", () => {
  const { photoSourceSuffix } = loadTableUtils();
  assert.equal(
    photoSourceSuffix("https://EN.Wikipedia.ORG/wiki/Foo"),
    " via Wikipedia",
  );
});
