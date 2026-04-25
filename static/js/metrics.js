/* metrics.js — receiver metrics time-series charts (uPlot) */
"use strict";

// ---------------------------------------------------------------------------
// Metric metadata: label, color, unit hint
// ---------------------------------------------------------------------------

const SERIES_META = {
  signal:            { label: "Signal",           color: "#4f8ef7" },
  noise:             { label: "Noise",            color: "#8891aa" },
  peak_signal:       { label: "Peak signal",      color: "#eab308" },
  strong_signals:    { label: "Strong (>-3 dBFS)",color: "#ef4444" },
  ac_with_pos:       { label: "With position",    color: "#22c55e" },
  ac_without_pos:    { label: "Without position",  color: "#8891aa" },
  ac_adsb:           { label: "ADS-B",            color: "#4f8ef7" },
  ac_mlat:           { label: "MLAT",             color: "#a855f7" },
  messages:          { label: "Messages",         color: "#4f8ef7" },
  local_modes:       { label: "Mode S",           color: "#22c55e" },
  local_bad:         { label: "Bad",              color: "#ef4444" },
  max_distance_m:    { label: "Max range",        color: "#4f8ef7" },
  positions_total:   { label: "Total",            color: "#4f8ef7" },
  positions_adsb:    { label: "ADS-B",            color: "#22c55e" },
  positions_mlat:    { label: "MLAT",             color: "#a855f7" },
  cpu_demod:         { label: "Demod",            color: "#4f8ef7" },
  cpu_reader:        { label: "Reader",           color: "#22c55e" },
  cpu_background:    { label: "Background",       color: "#a855f7" },
  cpu_aircraft_json: { label: "JSON gen",         color: "#eab308" },
  cpu_heatmap:       { label: "Heatmap",          color: "#f97316" },
  remote_accepted:   { label: "Accepted",         color: "#22c55e" },
  remote_bad:        { label: "Bad",              color: "#ef4444" },
  remote_modes:      { label: "Mode S",           color: "#4f8ef7" },
  remote_bytes_in:   { label: "Bytes in",         color: "#4f8ef7" },
  remote_bytes_out:  { label: "Bytes out",        color: "#22c55e" },
  tracks_new:        { label: "New tracks",       color: "#4f8ef7" },
  tracks_single:     { label: "Single-msg",       color: "#ef4444" },
  cpr_global_ok:     { label: "Global OK",        color: "#22c55e" },
  cpr_global_bad:    { label: "Global bad",       color: "#ef4444" },
  cpr_global_range:  { label: "Range reject",     color: "#f97316" },
  cpr_global_speed:  { label: "Speed reject",     color: "#eab308" },
  cpr_global_skipped:{ label: "Skipped",          color: "#8891aa" },
  cpr_airborne:      { label: "Airborne",         color: "#4f8ef7" },
  cpr_local_ok:      { label: "Local OK",         color: "#a855f7" },
  cpr_local_range:   { label: "Local range rej",  color: "#f97316" },
  cpr_local_speed:   { label: "Local speed rej",  color: "#eab308" },
  cpr_filtered:      { label: "Filtered",         color: "#8891aa" },
  local_accepted_0:  { label: "0-bit corr",       color: "#22c55e" },
  local_accepted_1:  { label: "1-bit corr",       color: "#eab308" },
  local_unknown_icao:{ label: "Unknown ICAO",     color: "#f97316" },
  samples_dropped:   { label: "Dropped",          color: "#ef4444" },
  samples_lost:      { label: "Lost",             color: "#f97316" },
  altitude_suppressed:{ label: "Alt suppressed",  color: "#8891aa" },
};

// ---------------------------------------------------------------------------
// Chart group definitions
// ---------------------------------------------------------------------------

const CHART_GROUPS = [
  {
    id: "signal",
    metrics: ["signal", "noise", "peak_signal"],
    axisLabel: "dBFS",
  },
  {
    id: "aircraft",
    metrics: ["ac_with_pos", "ac_without_pos", "ac_adsb", "ac_mlat"],
    axisLabel: "Aircraft",
  },
  {
    id: "messages",
    metrics: ["messages", "local_accepted_0", "local_accepted_1"],
    axisLabel: "Count / min",
  },
  {
    id: "range",
    metrics: ["max_distance_m"],
    axisLabel: "Range",
    valueFormat: formatRange,
  },
  {
    id: "positions",
    metrics: ["positions_total", "positions_adsb", "positions_mlat"],
    axisLabel: "Positions",
  },
  {
    id: "cpu",
    metrics: ["cpu_demod", "cpu_reader", "cpu_background", "cpu_aircraft_json", "cpu_heatmap"],
    axisLabel: "ms",
  },
  {
    id: "network-traffic",
    metrics: ["remote_bytes_out"],
    axisLabel: "Bytes / min",
    valueFormat: formatBytes,
  },
  {
    id: "network-in",
    metrics: ["remote_bytes_in"],
    axisLabel: "Bytes / min",
    valueFormat: formatBytes,
  },
  {
    id: "tracks",
    metrics: ["tracks_new", "tracks_single"],
    axisLabel: "Tracks",
  },
  {
    id: "decoder",
    metrics: ["local_modes", "local_bad"],
    axisLabel: "Count / min",
  },
  {
    id: "cpr",
    metrics: ["cpr_global_ok", "cpr_airborne", "cpr_local_ok"],
    axisLabel: "Count / min",
  },
];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  from: 0,
  to: 0,
  charts: {},        // id -> uPlot instance
};

// ---------------------------------------------------------------------------
// Range helpers
// ---------------------------------------------------------------------------

const RANGE_PRESETS = {
  "1h":  3600,
  "6h":  21600,
  "24h": 86400,
  "48h": 172800,
  "7d":  604800,
  "30d": 2592000,
  "90d": 7776000,
};

function rangeFromPreset(preset) {
  const now = Math.floor(Date.now() / 1000);
  return { from: now - (RANGE_PRESETS[preset] || 86400), to: now };
}

// Format a unix epoch in 24-hour clock; show date prefix only for spans
// longer than a single calendar day's worth of data.
function formatLegendTime(epoch, span) {
  const d = new Date(epoch * 1000);
  const time = d.toLocaleTimeString(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  if (span <= 86400) return time;
  const date = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return date + " " + time;
}

// ---------------------------------------------------------------------------
// Value formatters
// ---------------------------------------------------------------------------

function formatRange(val) {
  if (val == null) return "—";
  const units = localStorage.getItem("rsbs_units") || "metric";
  if (units === "metric") return (val / 1000).toFixed(1) + " km";
  if (units === "imperial") return (val / 1609.344).toFixed(1) + " mi";
  return (val / 1852).toFixed(1) + " nm";
}

function formatBytes(val) {
  if (val == null) return "—";
  if (Math.abs(val) < 1024 * 1024) return (val / 1024).toFixed(1) + " KB";
  return (val / 1024 / 1024).toFixed(1) + " MB";
}

function formatNum(val) {
  if (val == null) return "—";
  if (Number.isInteger(val)) return val.toLocaleString();
  return val.toFixed(2);
}

// ---------------------------------------------------------------------------
// Shared uPlot options
// ---------------------------------------------------------------------------

const GRID_STYLE = { stroke: "rgba(46,51,80,0.6)", width: 1 };
const TICK_STYLE = { stroke: "rgba(46,51,80,0.8)", width: 1 };

function baseOpts(group, width) {
  const series = [{
    label: "Time",
    value: (u, ts) => ts == null ? "—" : formatLegendTime(ts, state.to - state.from),
  }];
  for (const m of group.metrics) {
    const meta = SERIES_META[m] || { label: m, color: "#4f8ef7" };
    series.push({
      label: meta.label,
      stroke: meta.color,
      width: 1.5,
      value: (u, v) => (group.valueFormat || formatNum)(v),
    });
  }

  return {
    width: width,
    height: 220,
    cursor: { drag: { x: true, y: false } },
    select: { show: true },
    legend: { show: true },
    series: series,
    axes: [
      {
        stroke: "#8891aa",
        grid: GRID_STYLE,
        ticks: TICK_STYLE,
        values: (u, vals) => vals.map(v => {
          const d = new Date(v * 1000);
          const span = state.to - state.from;
          if (span <= 86400)
            return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
          return d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
            + "\n" + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
        }),
      },
      {
        stroke: "#8891aa",
        grid: GRID_STYLE,
        ticks: TICK_STYLE,
        label: group.axisLabel || "",
        values: (u, vals) => vals.map(v => (group.valueFormat || formatNum)(v)),
        size: 70,
      },
    ],
    hooks: {
      setSelect: [
        function(u) {
          const left  = u.posToVal(u.select.left,  "x");
          const right = u.posToVal(u.select.left + u.select.width, "x");
          if (right - left > 30) {
            state.from = Math.floor(left);
            state.to = Math.floor(right);
            syncRangeButtons();
            loadAll();
          }
          u.setSelect({ left: 0, width: 0, top: 0, height: 0 }, false);
        }
      ],
    },
  };
}

// ---------------------------------------------------------------------------
// Fetch + render
// ---------------------------------------------------------------------------

async function fetchGroup(group) {
  const metricsParam = group.metrics.join(",");
  const url = ROOT + "/api/metrics?from=" + state.from + "&to=" + state.to + "&metrics=" + encodeURIComponent(metricsParam);
  try {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

function renderChart(group, data) {
  const container = document.getElementById("chart-" + group.id);
  if (!container) return;

  // Destroy previous instance
  if (state.charts[group.id]) {
    state.charts[group.id].destroy();
    state.charts[group.id] = null;
  }

  if (!data || !data.data || data.data.length === 0 || data.data[0].length === 0) {
    container.innerHTML = '<p class="chart-empty">No data for this range</p>';
    return;
  }

  container.innerHTML = "";
  const width = container.clientWidth || 600;
  const opts = baseOpts(group, width);
  const chart = new uPlot(opts, data.data, container);
  state.charts[group.id] = chart;
}

async function loadAll() {
  const grid = document.getElementById("metrics-grid");
  if (!grid) return;

  const promises = CHART_GROUPS.map(async group => {
    const result = await fetchGroup(group);
    renderChart(group, result);
  });
  await Promise.all(promises);
}

// ---------------------------------------------------------------------------
// Range picker
// ---------------------------------------------------------------------------

function syncRangeButtons() {
  const now = Math.floor(Date.now() / 1000);
  const span = state.to - state.from;
  let matched = null;

  // Match if to is within 120s of now and span is within 5% of a preset
  if (Math.abs(now - state.to) < 120) {
    for (const [key, val] of Object.entries(RANGE_PRESETS)) {
      if (Math.abs(span - val) / val < 0.05) { matched = key; break; }
    }
  }

  document.querySelectorAll("#metrics-range .range-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.range === matched);
  });

  const customEl = document.getElementById("range-custom");
  if (matched) {
    customEl.classList.add("hidden");
  }
}

function initRangePicker() {
  document.querySelectorAll("#metrics-range .range-btn").forEach(btn => {
    btn.addEventListener("click", function() {
      const range = this.dataset.range;
      if (range === "custom") {
        const customEl = document.getElementById("range-custom");
        customEl.classList.toggle("hidden");
        if (!customEl.classList.contains("hidden")) {
          const fromInput = document.getElementById("range-from");
          const toInput = document.getElementById("range-to");
          fromInput.value = epochToLocal(state.from);
          toInput.value = epochToLocal(state.to);
        }
        return;
      }
      const r = rangeFromPreset(range);
      state.from = r.from;
      state.to = r.to;
      document.getElementById("range-custom").classList.add("hidden");
      syncRangeButtons();
      loadAll();
    });
  });

  document.getElementById("range-apply").addEventListener("click", function() {
    const fromInput = document.getElementById("range-from");
    const toInput = document.getElementById("range-to");
    const from = Math.floor(new Date(fromInput.value).getTime() / 1000);
    const to = Math.floor(new Date(toInput.value).getTime() / 1000);
    if (isNaN(from) || isNaN(to) || to <= from) return;
    state.from = from;
    state.to = to;
    syncRangeButtons();
    loadAll();
  });
}

function epochToLocal(epoch) {
  const d = new Date(epoch * 1000);
  const pad = n => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
    + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

// ---------------------------------------------------------------------------
// Resize handling
// ---------------------------------------------------------------------------

function handleResize() {
  for (const group of CHART_GROUPS) {
    const chart = state.charts[group.id];
    if (!chart) continue;
    const container = document.getElementById("chart-" + group.id);
    if (!container) continue;
    const width = container.clientWidth;
    if (width > 0) chart.setSize({ width: width, height: 220 });
  }
}

let resizeTimer = null;
window.addEventListener("resize", function() {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(handleResize, 150);
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", function() {
  const r = rangeFromPreset("24h");
  state.from = r.from;
  state.to = r.to;
  initRangePicker();
  loadAll();
});

// Re-render range chart when units change
document.addEventListener("unitschange", function() {
  const rangeGroup = CHART_GROUPS.find(g => g.id === "range");
  if (rangeGroup && state.charts.range) {
    // Refetch to apply new formatting in tooltips/axes
    fetchGroup(rangeGroup).then(result => renderChart(rangeGroup, result));
  }
});
