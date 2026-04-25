/* Unit tests for static/js/units.js — pure formatter functions.
   Run with: node --test tests/js/ */

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadJs, makeLocalStorage } from "./_loader.mjs";

function loadWithUnits(unit = "metric") {
  const localStorage = makeLocalStorage(unit ? { rsbs_units: unit } : {});
  // dispatchEvent is called from setUnits(); stub it to a no-op.
  const window = { dispatchEvent: () => true };
  const ctx = loadJs("static/js/units.js", { localStorage, window, CustomEvent: class {} });
  return ctx;
}

// ---------------------------------------------------------------------------
// fmtAlt — input is feet
// ---------------------------------------------------------------------------

test("fmtAlt: null returns em-dash", () => {
  const { fmtAlt } = loadWithUnits();
  assert.equal(fmtAlt(null), "—");
});

test("fmtAlt: metric converts feet to metres", () => {
  const { fmtAlt } = loadWithUnits("metric");
  // 10000 ft * 0.3048 = 3048 m
  assert.equal(fmtAlt(10000), "3,048 m");
});

test("fmtAlt: aeronautical keeps feet", () => {
  const { fmtAlt } = loadWithUnits("aeronautical");
  assert.equal(fmtAlt(35000), "35,000 ft");
});

test("fmtAlt: imperial uses feet", () => {
  const { fmtAlt } = loadWithUnits("imperial");
  assert.equal(fmtAlt(35000), "35,000 ft");
});

test("fmtAlt: showUnit=false omits unit suffix", () => {
  const { fmtAlt } = loadWithUnits("metric");
  assert.equal(fmtAlt(10000, false), "3,048");
});

// ---------------------------------------------------------------------------
// fmtSpd — input is knots
// ---------------------------------------------------------------------------

test("fmtSpd: null returns em-dash", () => {
  const { fmtSpd } = loadWithUnits();
  assert.equal(fmtSpd(null), "—");
});

test("fmtSpd: metric converts knots to km/h", () => {
  const { fmtSpd } = loadWithUnits("metric");
  // 100 kts * 1.852 = 185.2 → rounds to 185
  assert.equal(fmtSpd(100), "185 km/h");
});

test("fmtSpd: imperial converts knots to mph", () => {
  const { fmtSpd } = loadWithUnits("imperial");
  // 100 kts * 1.15078 = 115.078 → 115
  assert.equal(fmtSpd(100), "115 mph");
});

test("fmtSpd: aeronautical keeps knots", () => {
  const { fmtSpd } = loadWithUnits("aeronautical");
  assert.equal(fmtSpd(100), "100 kts");
});

// ---------------------------------------------------------------------------
// fmtDist — input is nautical miles
// ---------------------------------------------------------------------------

test("fmtDist: null returns em-dash", () => {
  const { fmtDist } = loadWithUnits();
  assert.equal(fmtDist(null), "—");
});

test("fmtDist: metric converts nm to km with one decimal", () => {
  const { fmtDist } = loadWithUnits("metric");
  // 100 nm * 1.852 = 185.2
  assert.equal(fmtDist(100), "185.2 km");
});

test("fmtDist: imperial converts nm to mi", () => {
  const { fmtDist } = loadWithUnits("imperial");
  assert.equal(fmtDist(100), "115.1 mi"); // 100 * 1.15078 → 115.078 → toFixed(1) = "115.1"
});

test("fmtDist: aeronautical keeps nm with one decimal", () => {
  const { fmtDist } = loadWithUnits("aeronautical");
  assert.equal(fmtDist(123.45), "123.5 nm");
});

// ---------------------------------------------------------------------------
// fmtClimb — input is feet/minute
// ---------------------------------------------------------------------------

test("fmtClimb: null returns em-dash", () => {
  const { fmtClimb } = loadWithUnits();
  assert.equal(fmtClimb(null), "—");
});

test("fmtClimb: positive value gets + prefix", () => {
  const { fmtClimb } = loadWithUnits("aeronautical");
  assert.equal(fmtClimb(1500), "+1,500 fpm");
});

test("fmtClimb: negative value keeps minus, no plus prefix", () => {
  const { fmtClimb } = loadWithUnits("aeronautical");
  assert.equal(fmtClimb(-1500), "-1,500 fpm");
});

test("fmtClimb: zero gets no prefix (current behaviour)", () => {
  const { fmtClimb } = loadWithUnits("aeronautical");
  // 0 > 0 is false → no plus; matches current implementation
  assert.equal(fmtClimb(0), "0 fpm");
});

test("fmtClimb: metric converts fpm to m/min", () => {
  const { fmtClimb } = loadWithUnits("metric");
  // 1000 fpm * 0.3048 = 304.8 → 305
  assert.equal(fmtClimb(1000), "+305 m/min");
});

// ---------------------------------------------------------------------------
// Label functions
// ---------------------------------------------------------------------------

test("altLabel: metric vs aeronautical", () => {
  assert.equal(loadWithUnits("metric").altLabel(), "Alt (m)");
  assert.equal(loadWithUnits("aeronautical").altLabel(), "Alt (ft)");
});

test("spdLabel: all three units", () => {
  assert.equal(loadWithUnits("metric").spdLabel(), "Speed (km/h)");
  assert.equal(loadWithUnits("imperial").spdLabel(), "Speed (mph)");
  assert.equal(loadWithUnits("aeronautical").spdLabel(), "Speed (kts)");
});

test("distLabel: all three units", () => {
  assert.equal(loadWithUnits("metric").distLabel(), "Dist (km)");
  assert.equal(loadWithUnits("imperial").distLabel(), "Dist (mi)");
  assert.equal(loadWithUnits("aeronautical").distLabel(), "Dist (nm)");
});

test("climbLabel: metric vs aeronautical", () => {
  assert.equal(loadWithUnits("metric").climbLabel(), "V/S (m/min)");
  assert.equal(loadWithUnits("aeronautical").climbLabel(), "V/S (fpm)");
});

// ---------------------------------------------------------------------------
// getUnits / setUnits — default and persistence
// ---------------------------------------------------------------------------

test("getUnits: defaults to metric when not set", () => {
  const { getUnits } = loadJs("static/js/units.js", {
    localStorage: makeLocalStorage({}),
    window: { dispatchEvent: () => true },
    CustomEvent: class {},
  });
  assert.equal(getUnits(), "metric");
});

test("setUnits: persists to localStorage and dispatches event", () => {
  const localStorage = makeLocalStorage({});
  let dispatched = null;
  const window = { dispatchEvent: (e) => { dispatched = e; return true; } };
  const ctx = loadJs("static/js/units.js", {
    localStorage,
    window,
    CustomEvent: class { constructor(name, init) { this.type = name; this.detail = init?.detail; } },
  });
  ctx.setUnits("imperial");
  assert.equal(localStorage.getItem("rsbs_units"), "imperial");
  assert.equal(dispatched.type, "unitschange");
  assert.equal(dispatched.detail, "imperial");
});
