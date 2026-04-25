/* stats.js — statistics page */

let statsData = null;

// ---------- Range picker ----------

let currentRange = { from: null, to: null };

function tsToDateStr(ts) {
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

function dateStrToTs(dateStr, endOfDay = false) {
  const [year, month, day] = dateStr.split("-").map(Number);
  const ts = Date.UTC(year, month - 1, day) / 1000;
  return endOfDay ? ts + 86399 : ts;
}

function getPresetRange(preset) {
  const now = Math.floor(Date.now() / 1000);
  const today = new Date();
  switch (preset) {
    case "7d":  return { from: now - 7 * 86400,  to: now };
    case "30d": return { from: now - 30 * 86400, to: now };
    case "month": {
      const from = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1) / 1000;
      return { from, to: now };
    }
    case "prev_month": {
      const from = Date.UTC(today.getUTCFullYear(), today.getUTCMonth() - 1, 1) / 1000;
      const to   = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1) / 1000 - 1;
      return { from, to };
    }
    default: return { from: null, to: null };
  }
}

function setActiveBtn(preset) {
  document.querySelectorAll(".range-btn").forEach(btn => btn.classList.remove("active"));
  const activeBtn = document.querySelector(`.range-btn[data-range="${preset}"]`);
  if (activeBtn) activeBtn.classList.add("active");
}

function applyRange(from, to, preset = null) {
  currentRange = { from, to };
  setActiveBtn(preset || (from || to ? null : "all"));
  const params = new URLSearchParams();
  if (from) params.set("from", from);
  if (to)   params.set("to",   to);
  const qs = params.toString();
  history.replaceState({}, "", qs ? "?" + qs : location.pathname);
  load();
}

function initRangePicker() {
  // Restore from URL
  const params = new URLSearchParams(location.search);
  const urlFrom = params.get("from");
  const urlTo   = params.get("to");
  if (urlFrom || urlTo) {
    currentRange = {
      from: urlFrom ? parseInt(urlFrom) : null,
      to:   urlTo   ? parseInt(urlTo)   : null,
    };
    setActiveBtn(null);
    document.getElementById("range-custom").classList.remove("hidden");
    if (currentRange.from) document.getElementById("range-from").value = tsToDateStr(currentRange.from);
    if (currentRange.to)   document.getElementById("range-to").value   = tsToDateStr(currentRange.to);
  }

  document.querySelectorAll(".range-btn[data-range]").forEach(btn => {
    btn.addEventListener("click", () => {
      const preset = btn.dataset.range;
      if (preset === "custom") {
        document.getElementById("range-custom").classList.remove("hidden");
        setActiveBtn("custom");
        return;
      }
      if (preset === "all") {
        document.getElementById("range-custom").classList.add("hidden");
        applyRange(null, null, "all");
        return;
      }
      document.getElementById("range-custom").classList.add("hidden");
      const range = getPresetRange(preset);
      applyRange(range.from, range.to, preset);
    });
  });

  let applyTimer = null;
  document.getElementById("range-apply").addEventListener("click", () => {
    clearTimeout(applyTimer);
    applyTimer = setTimeout(() => {
      const fromVal = document.getElementById("range-from").value;
      const toVal   = document.getElementById("range-to").value;
      if (!fromVal && !toVal) return;
      const from = fromVal ? dateStrToTs(fromVal, false) : null;
      const to   = toVal   ? dateStrToTs(toVal,   true)  : null;
      applyRange(from, to, "custom");
    }, 300);
  });
}

// ---------- Chart renderers ----------

function barChart(containerId, rows, labelKey, valueKey, secondaryKey, nameKey, linkPrefix) {
  const container = document.getElementById(containerId);
  if (!rows || rows.length === 0) { container.textContent = "No data yet."; return; }
  const max = Math.max(...rows.map(row => row[valueKey]));
  container.innerHTML = rows.map(row => {
    const pct = max > 0 ? (row[valueKey] / max * 100).toFixed(1) : 0;
    const code = linkPrefix
      ? `<a href="${ROOT}/history?${linkPrefix}=${encodeURIComponent(row[labelKey])}">${escHtml(row[labelKey])}</a>`
      : escHtml(row[labelKey]);
    const name = (nameKey && row[nameKey]) ? `<div class="bar-name">${escHtml(row[nameKey])}</div>` : "";
    const tipText = (nameKey && row[nameKey]) ? `${row[labelKey]} — ${row[nameKey]}` : row[labelKey];
    const secondary = secondaryKey
      ? ` <span class="secondary-count">(${row[secondaryKey].toLocaleString()} a/c)</span>`
      : "";
    return `
      <div class="bar-row">
        <div class="bar-label" title="${escHtml(tipText)}">${code}${name}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
        <div class="bar-value">${row[valueKey].toLocaleString()}${secondary}</div>
      </div>`;
  }).join("");
}

function sourceChart(breakdown) {
  const container = document.getElementById("source-chart");
  const items = [
    { label: "ADS-B", pct: breakdown.adsb  || 0, cls: "src-adsb"  },
    { label: "MLAT",  pct: breakdown.mlat  || 0, cls: "src-mlat"  },
    { label: "Other", pct: breakdown.other || 0, cls: "src-other" },
  ];
  container.innerHTML = items.map(item => `
    <div class="source-row">
      <div class="source-label">${item.label}</div>
      <div class="bar-track source-track">
        <div class="bar-fill ${item.cls}" style="width:${item.pct}%"></div>
      </div>
      <div class="bar-value">${item.pct}%</div>
    </div>
  `).join("");
}

function hourlyChart(dist) {
  const container = document.getElementById("hourly-chart");
  container.className = "hourly-chart";
  const max = Math.max(...dist.map(bin => bin.count), 1);
  container.innerHTML = dist.map(bin => {
    const hour = String(bin.hour).padStart(2, "0");
    const pct = (bin.count / max * 100).toFixed(1);
    return `<div class="hourly-bar" style="height:${pct}%" title="${hour}:00 — ${bin.count.toLocaleString()} flights" data-label="${hour}"></div>`;
  }).join("");
}

function dailyChart(days) {
  const container = document.getElementById("daily-chart");
  if (!days || days.length === 0) { container.textContent = "No data yet."; return; }
  const max = Math.max(...days.map(day => day.unique_aircraft), 1);
  container.innerHTML = days.map(day => {
    const pct = (day.unique_aircraft / max * 100).toFixed(1);
    return `
      <div class="daily-row">
        <div class="daily-label">${day.day}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
        <div class="bar-value bar-value-sm">${day.unique_aircraft}</div>
      </div>`;
  }).join("");
}

function altChart(dist) {
  const container = document.getElementById("alt-chart");
  const max = Math.max(...dist.map(band => band.count), 1);
  container.innerHTML = dist.map(band => {
    const pct = (band.count / max * 100).toFixed(1);
    return `
      <div class="bar-row">
        <div class="bar-label">${band.band}</div>
        <div class="bar-track"><div class="bar-fill bar-fill-purple" style="width:${pct}%"></div></div>
        <div class="bar-value">${band.count.toLocaleString()}</div>
      </div>`;
  }).join("");
}

function renderFurthest(flight) {
  const card = document.getElementById("furthest-card");
  if (!flight || flight.max_distance_nm == null) {
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
  card.innerHTML = `
    <div class="card-value" id="s-dist">${fmtDist(flight.max_distance_nm)}</div>
    <div class="card-label">Furthest detected</div>
    <div class="furthest-detail">
      <a href="${ROOT}/flight/${parseInt(flight.id)}">
        ${[flight.callsign, flight.registration, flight.aircraft_type].filter(Boolean).map(escHtml).join(" / ") || escHtml(flight.icao_hex)}
      </a>
      <span class="furthest-ts">${fmtTs(flight.first_seen)}</span>
    </div>
  `;
}

function squawkStats(counts) {
  const container = document.getElementById("squawk-chart");
  const codes = [
    { code: "7700", label: "General emergency",       color: "var(--red)"    },
    { code: "7600", label: "Radio failure (NORDO)",   color: "var(--yellow)" },
    { code: "7500", label: "Unlawful interference",   color: "var(--red)"    },
  ];
  container.innerHTML = codes.map(({ code, label, color }) => {
    const count = counts?.[code] ?? 0;
    const countEl = count > 0
      ? `<a href="${ROOT}/history?squawk=${code}" class="squawk-link squawk-active" style="color:${color}">${count.toLocaleString()}</a>`
      : `<span class="text-dim">0</span>`;
    return `
      <div class="squawk-row">
        <code class="squawk-code">${code}</code>
        <span class="squawk-label">${label}</span>
        <span class="squawk-count">${countEl}</span>
      </div>`;
  }).join("");
}

function newAircraftList(data) {
  const subhead = document.getElementById("new-aircraft-subhead");
  const container = document.getElementById("new-aircraft-list");

  if (!data || data.total === 0) {
    subhead.textContent = "(last 24h)";
    container.innerHTML = '<div class="no-data-msg">No new aircraft in the last 24h.</div>';
    return;
  }

  subhead.textContent = `(last 24h — ${data.total.toLocaleString()} total)`;

  const rows = data.items.map(aircraft => {
    const badge = (aircraft.flags & 1) ? ' <span class="badge badge-mil" title="Military">MIL</span>'
                : (aircraft.flags & 2) ? ' <span class="badge badge-int" title="Interesting">★</span>' : "";
    const typeStr = aircraft.aircraft_type
      ? (aircraft.type_desc ? `${escHtml(aircraft.aircraft_type)} ${escHtml(aircraft.type_desc)}` : escHtml(aircraft.aircraft_type))
      : "—";
    const typeEl = `<span class="na-type${!aircraft.aircraft_type ? ' text-dim' : ''}">${typeStr}</span>`;
    const time = fmtTs(aircraft.first_seen_ever).split(" ")[1] || ""; // HH:MM only
    return `
      <div class="na-row">
        <span class="na-time">${time}</span>
        <a href="${ROOT}/aircraft/${encodeURIComponent(aircraft.icao_hex)}" class="na-icao"><code>${escHtml(aircraft.icao_hex)}</code></a>
        <span class="na-reg">${escHtml(aircraft.registration) || "—"}</span>
        ${typeEl}${badge}
      </div>`;
  }).join("");

  const more = data.total > data.items.length
    ? `<div class="na-more">and ${(data.total - data.items.length).toLocaleString()} more…</div>`
    : "";

  container.innerHTML = rows + more;
}

function renderDelta(elId, curr, prev) {
  const container = document.getElementById(elId);
  if (!container) return;
  if (!prev) { container.textContent = ""; return; }
  const pct = Math.round((curr - prev) / prev * 100);
  if (pct === 0) {
    container.innerHTML = '<span class="delta-neutral">= prev period</span>';
  } else if (pct > 0) {
    container.innerHTML = `<span class="text-green">↑${pct}% vs prev</span>`;
  } else {
    container.innerHTML = `<span class="text-red">↓${Math.abs(pct)}% vs prev</span>`;
  }
}

function heatmapChart(data) {
  const container = document.getElementById("heatmap-chart");
  if (!data || data.length === 0) { container.textContent = "No data yet."; return; }

  // Build 7x24 matrix (dow 0=Sun ... 6=Sat, hour 0...23)
  const matrix = Array.from({length: 7}, () => new Array(24).fill(0));
  let maxCount = 0;
  for (const row of data) {
    matrix[row.dow][row.hour] = row.count;
    if (row.count > maxCount) maxCount = row.count;
  }

  const dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  let html = '<div class="heatmap-grid">';

  // Header row: empty corner + hour labels (show every 3rd)
  html += '<div class="heatmap-corner"></div>';
  for (let hour = 0; hour < 24; hour++) {
    const label = hour % 3 === 0 ? String(hour).padStart(2, "0") : "";
    html += `<div class="heatmap-hour-label">${label}</div>`;
  }

  // Data rows
  for (let dow = 0; dow < 7; dow++) {
    html += `<div class="heatmap-day-label">${dayNames[dow]}</div>`;
    for (let hour = 0; hour < 24; hour++) {
      const count = matrix[dow][hour];
      let style;
      if (count === 0 || maxCount === 0) {
        style = "background:var(--surface2)";
      } else {
        const opacity = Math.max(0.18, count / maxCount).toFixed(2);
        style = `background:rgba(79,142,247,${opacity})`;
      }
      const tip = `${dayNames[dow]} ${String(hour).padStart(2,"0")}:00 — ${count.toLocaleString()} flights`;
      html += `<div class="heatmap-cell" style="${style}" title="${tip}"></div>`;
    }
  }

  html += "</div>";
  container.innerHTML = html;
}

function frequentAircraftList(data) {
  const container = document.getElementById("frequent-aircraft-list");
  if (!data || data.length === 0) { container.textContent = "No data yet."; return; }
  const max = data[0].flights;
  container.innerHTML = data.map(aircraft => {
    const badge = (aircraft.flags & 1) ? ' <span class="badge badge-mil">MIL</span>'
                : (aircraft.flags & 2) ? ' <span class="badge badge-int">★</span>' : "";
    const reg  = escHtml(aircraft.registration || aircraft.icao_hex);
    const type = aircraft.aircraft_type
      ? (aircraft.type_desc ? `${escHtml(aircraft.aircraft_type)} · ${escHtml(aircraft.type_desc)}` : escHtml(aircraft.aircraft_type))
      : "—";
    const pct  = (aircraft.flights / max * 100).toFixed(1);
    return `
      <div class="fa-row">
        <a href="${ROOT}/aircraft/${encodeURIComponent(aircraft.icao_hex)}" class="fa-reg">${reg}</a>${badge}
        <div class="fa-type">${type}</div>
        <div class="bar-track fa-bar"><div class="bar-fill" style="width:${pct}%"></div></div>
        <div class="fa-count">${aircraft.flights}</div>
      </div>`;
  }).join("");
}

function countriesChart(data) {
  const container = document.getElementById("countries-chart");
  if (!data || data.length === 0) { container.textContent = "No data yet."; return; }
  const max = Math.max(...data.map(row => row.flights));
  container.innerHTML = data.map(row => {
    const pct = max > 0 ? (row.flights / max * 100).toFixed(1) : 0;
    const acStr = `<span class="secondary-count">(${row.unique_aircraft.toLocaleString()} a/c)</span>`;
    return `
      <div class="bar-row">
        <div class="bar-label" title="${escHtml(row.country)}">${escHtml(row.country)}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
        <div class="bar-value">${row.flights.toLocaleString()} ${acStr}</div>
      </div>`;
  }).join("");
}

function renderAll(data) {
  // Range indicator
  const rangeLabel = document.getElementById("range-active-label");
  if (data.range && (data.range.from || data.range.to)) {
    const from = data.range.from ? tsToDateStr(data.range.from) : "…";
    const to   = data.range.to   ? tsToDateStr(data.range.to)   : "now";
    rangeLabel.textContent = `${from} → ${to}`;
  } else {
    rangeLabel.textContent = "";
  }

  // Dynamic section subheads
  const dailySubhead = document.getElementById("daily-subhead");
  if (data.range && (data.range.from || data.range.to)) {
    const from = data.range.from ? tsToDateStr(data.range.from) : "…";
    const to   = data.range.to   ? tsToDateStr(data.range.to)   : "now";
    dailySubhead.textContent = `(${from} → ${to})`;
  } else {
    dailySubhead.textContent = "(last 30 days)";
  }

  // Summary cards
  document.getElementById("s-flights").textContent  = (data.total_flights   || 0).toLocaleString();
  document.getElementById("s-aircraft").textContent = (data.unique_aircraft  || 0).toLocaleString();
  document.getElementById("s-airlines").textContent = (data.unique_airlines  || 0).toLocaleString();
  document.getElementById("s-24h").textContent      = (data.flights_last_24h || 0).toLocaleString();
  document.getElementById("s-7d").textContent       = (data.flights_last_7d  || 0).toLocaleString();
  document.getElementById("s-db").textContent       = fmtBytes(data.db_size_bytes);
  document.getElementById("s-mil").textContent      = (data.military_flights     || 0).toLocaleString();
  document.getElementById("s-int").textContent      = (data.interesting_flights  || 0).toLocaleString();
  renderFurthest(data.furthest_aircraft);

  // Trend deltas
  if (data.trends) {
    renderDelta("d-24h", data.flights_last_24h || 0, data.trends.flights_24h_prev || 0);
    renderDelta("d-7d",  data.flights_last_7d  || 0, data.trends.flights_7d_prev  || 0);
  }

  // Charts
  squawkStats(data.squawk_counts);
  newAircraftList(data.new_aircraft);
  sourceChart(data.source_breakdown || {});
  barChart("airlines-chart", data.top_airlines,
    "airline", "flights", "unique_aircraft", "airline_name", "callsign");
  barChart("types-chart", data.top_aircraft_types,
    "type", "flights", "unique_aircraft", "type_desc", "aircraft_type");

  // Top routes: build a "WAW->LHR" label and show full names as tooltip
  const routeRows = (data.top_routes || []).map(route => ({
    ...route,
    route_label: route.origin_icao + "→" + route.dest_icao,
    route_name:  [route.origin_name, route.dest_name].filter(Boolean).join(" → "),
  }));
  barChart("routes-chart", routeRows, "route_label", "flights", null, "route_name", null);

  barChart("airports-chart", data.top_airports || [],
    "icao_code", "appearances", null, "name", null);

  hourlyChart(data.hourly_distribution || []);
  altChart(data.altitude_distribution || []);
  dailyChart(data.daily_unique_aircraft || []);
  heatmapChart(data.heatmap || []);
  countriesChart(data.top_countries || []);
  frequentAircraftList(data.frequent_aircraft || []);
}

// ---------- Polar range chart ----------

let polarBuckets = null;

function polarChart(buckets) {
  const container = document.getElementById("polar-chart");
  if (!buckets || buckets.length === 0) { container.textContent = "No data yet."; return; }

  const RADIUS = 190, CX = 260, CY = 265, SVG_W = 560, SVG_H = 540;

  const maxDist = Math.max(...buckets.map(bucket => bucket.max_dist_nm), 1);
  const ringStep = maxDist <= 100 ? 25 : 50;
  const ringMax  = Math.ceil(maxDist / ringStep) * ringStep;
  const scale    = RADIUS / ringMax;

  function distLabel(nm) {
    const units = getUnits();
    if (units === "metric")   return Math.round(nm * 1.852) + " km";
    if (units === "imperial") return Math.round(nm * 1.15078) + " mi";
    return nm + " nm";
  }

  // Background circle
  let svg = `<circle cx="${CX}" cy="${CY}" r="${RADIUS}" fill="var(--surface2)" stroke="none"/>`;

  // Radial grid lines every 10deg (very subtle)
  for (let angle = 0; angle < 360; angle += 10) {
    const rad = Math.PI * angle / 180;
    const x2 = (CX + RADIUS * Math.sin(rad)).toFixed(1);
    const y2 = (CY - RADIUS * Math.cos(rad)).toFixed(1);
    svg += `<line x1="${CX}" y1="${CY}" x2="${x2}" y2="${y2}" stroke="var(--border)" stroke-width="0.5"/>`;
  }

  // Concentric distance rings + labels on East axis
  for (let dist = ringStep; dist <= ringMax; dist += ringStep) {
    const ringR = (dist * scale).toFixed(1);
    svg += `<circle cx="${CX}" cy="${CY}" r="${ringR}" fill="none" stroke="var(--border)" stroke-width="1"/>`;
    // Labels placed along ~82deg (just clockwise of East) so they stagger vertically
    const labelR = dist * scale;
    const lx = (CX + labelR * Math.sin(Math.PI * 82 / 180) + 5).toFixed(1);
    const ly = (CY - labelR * Math.cos(Math.PI * 82 / 180) + 4).toFixed(1);
    svg += `<text x="${lx}" y="${ly}" class="polar-ring-label">${distLabel(dist)}</text>`;
  }

  // Cardinal direction labels
  const cardinals = [
    [0,"N"],[45,"NE"],[90,"E"],[135,"SE"],[180,"S"],[225,"SW"],[270,"W"],[315,"NW"]
  ];
  for (const [angle, name] of cardinals) {
    const rad = Math.PI * angle / 180;
    const lx = (CX + (RADIUS + 20) * Math.sin(rad)).toFixed(1);
    const ly = (CY - (RADIUS + 20) * Math.cos(rad) + 4).toFixed(1);
    svg += `<text x="${lx}" y="${ly}" class="polar-label" text-anchor="middle">${name}</text>`;
  }

  // Coverage polygon
  const pts = buckets.map(bucket => {
    const rad = Math.PI * bucket.bearing / 180;
    const ptR = bucket.max_dist_nm * scale;
    return `${(CX + ptR * Math.sin(rad)).toFixed(1)},${(CY - ptR * Math.cos(rad)).toFixed(1)}`;
  }).join(" ");
  svg += `<polygon points="${pts}" class="polar-polygon"/>`;

  // Receiver dot
  svg += `<circle cx="${CX}" cy="${CY}" r="3" fill="var(--accent)"/>`;

  container.innerHTML = `<svg viewBox="0 0 ${SVG_W} ${SVG_H}" class="polar-svg">${svg}</svg>`;
}

// ---------- Personal records ----------

let recordsData = null;

// Duration formatting: use global fmtDur() from base.html

function renderRecords(data) {
  const container = document.getElementById("records-grid");
  if (!data) { container.textContent = "No data yet."; return; }

  const items = [
    {
      flight: data.furthest,
      label:  "Furthest Detected",
      value:  data.furthest ? fmtDist(data.furthest.max_distance_nm) : null,
    },
    {
      flight: data.fastest,
      label:  "Fastest Recorded",
      value:  data.fastest ? fmtSpd(data.fastest.max_gs) : null,
    },
    {
      flight: data.highest,
      label:  "Highest Altitude",
      value:  data.highest ? fmtAlt(data.highest.max_alt_baro) : null,
    },
    {
      flight: data.longest,
      label:  "Longest Tracked",
      value:  data.longest ? (fmtDur(data.longest.duration_s) || "—") : null,
    },
  ];

  container.innerHTML = items.map(({ flight, label, value }) => {
    if (!flight) {
      return `<div class="record-card"><div class="record-value">—</div><div class="record-label">${label}</div></div>`;
    }
    const ident = [flight.callsign, flight.registration, flight.aircraft_type].filter(Boolean).map(escHtml).join(" / ") || escHtml(flight.icao_hex);
    return `
      <div class="record-card">
        <div class="record-value">${value}</div>
        <div class="record-label">${label}</div>
        <div class="record-detail">
          <a href="${ROOT}/flight/${parseInt(flight.id)}">${ident}</a>
          <span class="record-date">${fmtTs(flight.first_seen)}</span>
        </div>
      </div>`;
  }).join("");
}

async function load() {
  try {
    const params = new URLSearchParams();
    if (currentRange.from) params.set("from", currentRange.from);
    if (currentRange.to)   params.set("to",   currentRange.to);
    const qs = params.toString();

    const [statsResp, polarResp, recordsResp] = await Promise.all([
      fetch(ROOT + "/api/stats" + (qs ? "?" + qs : "")),
      fetch(ROOT + "/api/stats/polar"),
      fetch(ROOT + "/api/stats/records"),
    ]);
    if (!statsResp.ok) throw new Error("HTTP " + statsResp.status);
    statsData = await statsResp.json();
    renderAll(statsData);

    if (polarResp.ok) {
      const polarData = await polarResp.json();
      polarBuckets = polarData.buckets;
      polarChart(polarBuckets);
    }

    if (recordsResp.ok) {
      recordsData = await recordsResp.json();
      renderRecords(recordsData);
    }
  } catch (err) {
    document.querySelector(".summary-cards").insertAdjacentHTML(
      "afterend",
      `<p class="error-msg">Failed to load statistics: ${escHtml(err.message)}</p>`
    );
  }
}

// Re-render distance/unit-dependent parts on units change
window.addEventListener("unitschange", () => {
  if (statsData)    renderFurthest(statsData.furthest_aircraft);
  if (polarBuckets) polarChart(polarBuckets);
  if (recordsData)  renderRecords(recordsData);
});

// ---------- Collapsible sections ----------

const COLLAPSE_KEY = "rsbs_stats_collapsed";

function getCollapsed() {
  try { return JSON.parse(localStorage.getItem(COLLAPSE_KEY)) || []; }
  catch { return []; }
}

function saveCollapsed(list) {
  localStorage.setItem(COLLAPSE_KEY, JSON.stringify(list));
}

function initCollapsible() {
  const collapsed = getCollapsed();
  document.querySelectorAll(".stat-section[data-section]").forEach(section => {
    const key = section.dataset.section;
    if (collapsed.includes(key)) section.classList.add("collapsed");
    section.querySelector("h2").addEventListener("click", () => {
      section.classList.toggle("collapsed");
      const current = getCollapsed();
      if (section.classList.contains("collapsed")) {
        if (!current.includes(key)) current.push(key);
      } else {
        const idx = current.indexOf(key);
        if (idx !== -1) current.splice(idx, 1);
      }
      saveCollapsed(current);
    });
  });
}

initCollapsible();
initRangePicker();
load();
