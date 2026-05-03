/* Unit tests for static/js/flight_detail.js — only the pure helper
   `airspacePopup` is exercised here. The rest of the file drives Leaflet and
   the DOM, which would need a real browser to test. */

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadJs } from "./_loader.mjs";

// Real escHtml from base.html — copied verbatim so the test mirrors prod.
function escHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function loadFlightDetail() {
  // flight_detail.js runs `load()` and a `window.addEventListener` at the
  // bottom of the file. We stub just enough for those top-level calls to
  // succeed without errors; `load()` returns a rejected promise from the
  // stubbed fetch, which is harmless for our pure-function test.
  return loadJs("static/js/flight_detail.js", {
    // getElementById returns a sink object — both the success and error
    // paths of load() set .textContent on real DOM nodes, so a null return
    // would throw an unhandledRejection that node:test reports as a failure.
    document: {
      getElementById: () => ({ textContent: "", classList: { add: () => {}, remove: () => {} } }),
      querySelectorAll: () => [],
      createElement: () => ({ classList: { add: () => {} } }),
    },
    window: { addEventListener: () => {} },
    ROOT: "",
    FLIGHT_ID: "0",
    L: {},
    escHtml,
    // Never-resolving fetch keeps load() suspended forever — the airspacePopup
    // tests don't exercise it, and a resolved-but-failed fetch would leak an
    // unhandledRejection through the catch path's DOM access.
    fetch: () => new Promise(() => {}),
    loadPhoto: () => {},
    console: { error: () => {} },
  });
}

// ---------------------------------------------------------------------------
// airspacePopup — must escape every interpolated GeoJSON property.
// The popup string is passed to Leaflet's bindPopup() which renders it as HTML.
// ---------------------------------------------------------------------------

test("airspacePopup: escapes script tag in name", () => {
  const { airspacePopup } = loadFlightDetail();
  const html = airspacePopup({
    properties: { name: '<img src=x onerror=alert(1)>', type: "CTR" },
  });
  assert.equal(html.includes("<img"), false);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test("airspacePopup: escapes injection in type", () => {
  const { airspacePopup } = loadFlightDetail();
  const html = airspacePopup({
    properties: { name: "OK", type: '<script>alert(1)</script>' },
  });
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;script&gt;/);
});

test("airspacePopup: escapes injection in icaoClass", () => {
  const { airspacePopup } = loadFlightDetail();
  const html = airspacePopup({
    properties: { name: "OK", type: "CTR", icaoClass: '"><img src=x>' },
  });
  assert.equal(html.includes('"><img'), false);
  assert.match(html, /&quot;&gt;&lt;img/);
});

test("airspacePopup: escapes injection in upperLimit unit", () => {
  const { airspacePopup } = loadFlightDetail();
  const html = airspacePopup({
    properties: {
      name: "OK", type: "CTR",
      upperLimit: { value: 1000, unit: '<svg/onload=alert(1)>' },
    },
  });
  assert.equal(html.includes("<svg"), false);
  assert.match(html, /&lt;svg/);
});

test("airspacePopup: escapes injection in lowerLimit unit", () => {
  const { airspacePopup } = loadFlightDetail();
  const html = airspacePopup({
    properties: {
      name: "OK", type: "CTR",
      lowerLimit: { value: 0, unit: '"><script>alert(1)</script>' },
    },
  });
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;script&gt;/);
});

test("airspacePopup: benign data renders the expected HTML", () => {
  const { airspacePopup } = loadFlightDetail();
  const html = airspacePopup({
    properties: {
      name: "EPWA TMA",
      type: "TMA",
      icaoClass: "C",
      upperLimit: { value: 12500, unit: "FT" },
      lowerLimit: { value: 1500, unit: "FT" },
    },
  });
  assert.match(html, /<b>EPWA TMA<\/b>/);
  assert.match(html, /TMA \/ Class C/);
  assert.match(html, /1500 FT – 12500 FT/);
});

test("airspacePopup: missing lowerLimit defaults to GND", () => {
  const { airspacePopup } = loadFlightDetail();
  const html = airspacePopup({
    properties: {
      name: "X", type: "P",
      upperLimit: { value: 5000, unit: "FT" },
    },
  });
  assert.match(html, /GND – 5000 FT/);
});

test("airspacePopup: missing properties falls back to 'Airspace'", () => {
  const { airspacePopup } = loadFlightDetail();
  const html = airspacePopup({});
  assert.match(html, /<b>Airspace<\/b>/);
});
