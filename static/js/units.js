/* units.js — unit conversion helpers, loaded globally via base.html */

const _UNITS_KEY = "rsbs_units";

function getUnits() {
  return localStorage.getItem(_UNITS_KEY) || "metric";
}
function setUnits(u) {
  localStorage.setItem(_UNITS_KEY, u);
  window.dispatchEvent(new CustomEvent("unitschange", { detail: u }));
}

// ---------- Altitude (input: feet) ----------
function fmtAlt(ft, showUnit = true) {
  if (ft == null) return "—";
  if (getUnits() === "metric") {
    const m = Math.round(ft * 0.3048);
    return m.toLocaleString() + (showUnit ? " m" : "");
  }
  // aeronautical + imperial both use feet
  return Math.round(ft).toLocaleString() + (showUnit ? " ft" : "");
}

// ---------- Speed (input: knots) ----------
function fmtSpd(kts, showUnit = true) {
  if (kts == null) return "—";
  const u = getUnits();
  if (u === "metric")   return Math.round(kts * 1.852).toLocaleString()   + (showUnit ? " km/h" : "");
  if (u === "imperial") return Math.round(kts * 1.15078).toLocaleString() + (showUnit ? " mph"  : "");
  return Math.round(kts) + (showUnit ? " kts" : "");
}

// ---------- Distance (input: nautical miles) ----------
function fmtDist(nm, showUnit = true) {
  if (nm == null) return "—";
  const u = getUnits();
  if (u === "metric")   return (nm * 1.852).toFixed(1)   + (showUnit ? " km" : "");
  if (u === "imperial") return (nm * 1.15078).toFixed(1) + (showUnit ? " mi" : "");
  return nm.toFixed(1) + (showUnit ? " nm" : "");
}

// ---------- Climb rate (input: feet/min) ----------
function fmtClimb(fpm, showUnit = true) {
  if (fpm == null) return "—";
  const prefix = fpm > 0 ? "+" : "";
  if (getUnits() === "metric") {
    const mpm = Math.round(fpm * 0.3048);
    return prefix + mpm.toLocaleString() + (showUnit ? " m/min" : "");
  }
  return prefix + fpm.toLocaleString() + (showUnit ? " fpm" : "");
}

// ---------- Column labels ----------
function altLabel()  { return getUnits() === "metric" ? "Alt (m)"    : "Alt (ft)"; }
function spdLabel()  {
  const u = getUnits();
  return u === "metric" ? "Speed (km/h)" : u === "imperial" ? "Speed (mph)" : "Speed (kts)";
}
function distLabel() {
  const u = getUnits();
  return u === "metric" ? "Dist (km)" : u === "imperial" ? "Dist (mi)" : "Dist (nm)";
}
function climbLabel() { return getUnits() === "metric" ? "V/S (m/min)" : "V/S (fpm)"; }

// ---------- Units selector initialisation (called from base.html) ----------
function initUnitSelector() {
  const sel = document.getElementById("units-select");
  if (!sel) return;
  sel.value = getUnits();
  sel.addEventListener("change", e => setUnits(e.target.value));
}
