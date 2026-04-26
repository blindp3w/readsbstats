/* Unit tests for static/js/table-utils.js — pure helpers only.
   `flagBadge` is the only DOM-free function; the rest (initSortHeaders,
   renderPagination, loadPhoto) need a real document/fetch and aren't
   sensibly testable without a DOM emulator. */

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadJs } from "./_loader.mjs";

function loadTableUtils() {
  // table-utils.js references `document`, `ROOT`, `escHtml`, `fetch` at
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
// safeHttpUrl — URL scheme allowlist (XSS protection for href/src)
// ---------------------------------------------------------------------------

test("safeHttpUrl: accepts http:// URLs", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("http://example.com/foo"), "http://example.com/foo");
});

test("safeHttpUrl: accepts https:// URLs", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("https://example.com/foo"), "https://example.com/foo");
});

test("safeHttpUrl: accepts mixed-case schemes", () => {
  const { safeHttpUrl } = loadTableUtils();
  assert.equal(safeHttpUrl("HTTPS://example.com/"), "HTTPS://example.com/");
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
