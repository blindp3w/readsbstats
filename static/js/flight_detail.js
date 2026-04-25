/* flight_detail.js — single flight detail page with Leaflet map */

const PREVIEW_ROWS = 30;
const MAX_RENDER_ROWS = 5000;
let allPositions = [];
let flightData   = null;

function sourceColor(src) {
  if (!src) return "#8891aa";
  if (src === "mlat") return "#eab308";
  if (src.startsWith("adsb") || src.startsWith("adsr") || src === "adsc") return "#4f8ef7";
  return "#8891aa";
}

function airspaceStyle(feature) {
  const type = ((feature.properties || {}).type || "").toUpperCase();
  const styles = {
    CTR: { color: "#ef4444", fillColor: "#ef4444", fillOpacity: 0.07, weight: 1.5 },
    TMA: { color: "#3b82f6", fillColor: "#3b82f6", fillOpacity: 0.05, weight: 1.5 },
    D:   { color: "#f97316", fillColor: "#f97316", fillOpacity: 0.09, weight: 1.5 },
    R:   { color: "#dc2626", fillColor: "#dc2626", fillOpacity: 0.11, weight: 1.5 },
    P:   { color: "#991b1b", fillColor: "#991b1b", fillOpacity: 0.18, weight: 1.5 },
  };
  return styles[type] || { color: "#8b5cf6", fillColor: "#8b5cf6", fillOpacity: 0.06, weight: 1.5 };
}

function airspacePopup(feature) {
  const props = feature.properties || {};
  const name  = props.name  || "Airspace";
  const type  = props.type  || "";
  const cls   = props.icaoClass ? " / Class " + props.icaoClass : "";
  const fmtLim = lim => lim ? `${lim.value} ${lim.unit}` : "—";
  const upper = fmtLim(props.upperLimit);
  const lower = props.lowerLimit ? fmtLim(props.lowerLimit) : "GND";
  return `<b>${name}</b><br>${type}${cls}<br>${lower} – ${upper}`;
}

const _AIRSPACE_LEGEND = [
  { type: "CTR", color: "#ef4444", label: "CTR — Control Zone"      },
  { type: "TMA", color: "#3b82f6", label: "TMA — Terminal Area"      },
  { type: "D",   color: "#f97316", label: "D — Danger"               },
  { type: "R",   color: "#dc2626", label: "R — Restricted"           },
  { type: "P",   color: "#991b1b", label: "P — Prohibited"           },
];

async function addAirspaceOverlay(map, tileLayer) {
  try {
    const resp = await fetch(ROOT + "/api/airspace");
    if (!resp.ok) return;
    const geojson = await resp.json();
    if (!geojson.features || geojson.features.length === 0) return;

    const layer = L.geoJSON(geojson, {
      style: airspaceStyle,
      onEachFeature: (feature, lyr) => lyr.bindPopup(airspacePopup(feature)),
    }).addTo(map);

    L.control.layers(
      { "Map": tileLayer },
      { "Airspace": layer },
      { position: "topright", collapsed: false }
    ).addTo(map);

    // Render legend for zone types actually present in the data
    const presentTypes = new Set(
      geojson.features.map(feat => ((feat.properties || {}).type || "").toUpperCase())
    );
    const items = _AIRSPACE_LEGEND.filter(entry => presentTypes.has(entry.type));
    if (items.length > 0) {
      const legendEl = document.getElementById("airspace-legend");
      legendEl.innerHTML = items.map(entry =>
        `<span class="profile-dot" style="background:${entry.color}"></span>${entry.label}`
      ).join("&ensp;");
      legendEl.classList.remove("hidden");
    }
  } catch (err) { console.error("addAirspaceOverlay:", err); }
}

function initMap(positions, flight, receiverLat, receiverLon) {
  const mapEl = document.getElementById("flight-map");
  const map = L.map(mapEl, { preferCanvas: true });

  const tileLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors",
    maxZoom: 18,
  }).addTo(map);

  if (positions.length === 0) {
    map.setView([receiverLat, receiverLon], 6);
    return;
  }

  // Group consecutive positions by source type for color-coded segments
  const segments = [];
  let seg = [positions[0]];
  for (let idx = 1; idx < positions.length; idx++) {
    if (positions[idx].source_type !== positions[idx - 1].source_type) {
      segments.push({ src: positions[idx - 1].source_type, pts: seg });
      seg = [positions[idx - 1]];
    }
    seg.push(positions[idx]);
  }
  segments.push({ src: positions[positions.length - 1].source_type, pts: seg });

  segments.forEach(({ src, pts }) => {
    L.polyline(pts.map(pos => [pos.lat, pos.lon]), {
      color: sourceColor(src),
      weight: 2.5,
      opacity: 0.85,
      dashArray: src === "mlat" ? "6 4" : null,
    }).addTo(map);
  });

  // Clickable markers every ~200th point to keep map fast
  const step = Math.max(1, Math.floor(positions.length / 200));
  positions.forEach((pos, idx) => {
    if (idx % step !== 0 && idx !== positions.length - 1) return;
    L.circleMarker([pos.lat, pos.lon], {
      radius: 3,
      color: sourceColor(pos.source_type),
      fillColor: sourceColor(pos.source_type),
      fillOpacity: 0.8,
      weight: 1,
    })
      .bindPopup(
        // Map popups always use aeronautical units (feet / knots)
        `<b>${fmtTs(pos.ts)}</b><br>` +
        `Alt: ${pos.alt_baro != null ? pos.alt_baro.toLocaleString() + " ft" : "—"}<br>` +
        `Speed: ${pos.gs != null ? Math.round(pos.gs) + " kts" : "—"}<br>` +
        `Track: ${pos.track != null ? Math.round(pos.track) + "°" : "—"}<br>` +
        `Source: ${escHtml(pos.source_type || "—")}`
      )
      .addTo(map);
  });

  // Start / end markers
  const first = positions[0];
  const last  = positions[positions.length - 1];
  L.circleMarker([first.lat, first.lon], { radius: 7, color: "#22c55e", fillColor: "#22c55e", fillOpacity: 1, weight: 2 })
    .bindPopup("Start: " + fmtTs(first.ts)).addTo(map);
  L.circleMarker([last.lat, last.lon], { radius: 7, color: "#ef4444", fillColor: "#ef4444", fillOpacity: 1, weight: 2 })
    .bindPopup("End: " + fmtTs(last.ts)).addTo(map);

  if (flight.lat_min != null) {
    map.fitBounds([[flight.lat_min, flight.lon_min], [flight.lat_max, flight.lon_max]], { padding: [30, 30] });
  } else {
    map.fitBounds(positions.map(pos => [pos.lat, pos.lon]), { padding: [30, 30] });
  }

  // Load airspace overlay asynchronously — map is fully usable while it fetches
  addAirspaceOverlay(map, tileLayer);
}

function renderMeta(flight) {
  const badge = flagBadge(flight.flags, "long");
  const milRow = badge ? [["", badge.trim()]] : [];
  const rows = [
    ["ICAO",         `<a href="${ROOT}/aircraft/${encodeURIComponent(flight.icao_hex)}"><code>${escHtml(flight.icao_hex)}</code></a>`],
    ["Callsign",     escHtml(flight.callsign) || "—"],
    ["Airline",      escHtml(flight.airline_name) || "—"],
    ["Origin",       flight.origin_icao ? `${escHtml(flight.origin_icao)}${flight.origin_name ? " — " + escHtml(flight.origin_name) : ""}` : "—"],
    ["Destination",  flight.dest_icao   ? `${escHtml(flight.dest_icao)}${flight.dest_name     ? " — " + escHtml(flight.dest_name)   : ""}` : "—"],
    ["Registration", escHtml(flight.registration) || "—"],
    ["Type",         flight.aircraft_type ? `${escHtml(flight.aircraft_type)}${flight.type_desc ? " — " + escHtml(flight.type_desc) : ""}` : "—"],
    ...milRow,
    ["Squawk",       escHtml(flight.squawk) || "—"],
    ["Category",     escHtml(flight.category) || "—"],
    ["Source",       sourceBadge(flight.primary_source)],
    ["ADS-B pos.",   (flight.adsb_positions || 0).toLocaleString()],
    ["MLAT pos.",    (flight.mlat_positions || 0).toLocaleString()],
    ["First seen",   fmtTs(flight.first_seen)],
    ["Last seen",    fmtTs(flight.last_seen)],
    ["Duration",     fmtDur(flight.last_seen - flight.first_seen)],
    ["Max altitude", fmtAlt(flight.max_alt_baro)],
    ["Max speed",    fmtSpd(flight.max_gs)],
    ["Max distance", flight.max_distance_nm != null ? fmtDist(flight.max_distance_nm) : "—"],
    ["Positions",    (flight.total_positions || 0).toLocaleString()],
  ];
  const tbody = document.getElementById("meta-table");
  tbody.innerHTML = rows.map(([key, val]) => `<tr><td>${key}</td><td>${val}</td></tr>`).join("");
}

function rssiColor(rssi) {
  if (rssi == null) return "";
  // 0 to −3: very strong (green), −3 to −10: strong, −10 to −20: moderate (yellow), −20 to −30: weak, below −30: poor (red)
  if (rssi >= -3)  return "color:var(--green)";
  if (rssi >= -10) return "color:#6ee7a0";
  if (rssi >= -20) return "color:var(--yellow)";
  if (rssi >= -30) return "color:var(--orange)";
  return "color:var(--red)";
}

function srcBadgeSmall(src) {
  if (!src) return "—";
  if (src === "mlat") return sourceBadge("mlat");
  if (src.startsWith("adsb") || src.startsWith("adsr") || src === "adsc") return sourceBadge("adsb");
  return sourceBadge(src);
}

function renderPositions(positions, showAll) {
  const limit = showAll ? MAX_RENDER_ROWS : PREVIEW_ROWS;
  const visible = positions.slice(0, limit);
  const tbody = document.getElementById("pos-body");

  // Update column headers to reflect current units
  const thead = document.querySelector("#pos-table thead tr");
  if (thead) {
    thead.cells[1].textContent = altLabel();
    thead.cells[2].textContent = spdLabel();
    thead.cells[4].textContent = climbLabel();
  }

  tbody.innerHTML = visible.map(pos => `
    <tr>
      <td>${fmtTs(pos.ts)}</td>
      <td>${fmtAlt(pos.alt_baro)}</td>
      <td>${fmtSpd(pos.gs)}</td>
      <td>${pos.track != null ? Math.round(pos.track) + "°" : "—"}</td>
      <td>${fmtClimb(pos.baro_rate)}</td>
      <td style="${rssiColor(pos.rssi)}">${pos.rssi != null ? pos.rssi.toFixed(1) : "—"}</td>
      <td>${srcBadgeSmall(pos.source_type)}</td>
    </tr>
  `).join("");

  document.getElementById("pos-count").textContent = `(${positions.length.toLocaleString()})`;

  const showAllBtn = document.getElementById("show-all-btn");
  if (!showAll && positions.length > PREVIEW_ROWS) {
    showAllBtn.classList.remove("hidden");
    const label = positions.length > MAX_RENDER_ROWS
      ? `Show first ${MAX_RENDER_ROWS.toLocaleString()} of ${positions.length.toLocaleString()} positions`
      : `Show all ${positions.length.toLocaleString()} positions`;
    showAllBtn.textContent = label;
    showAllBtn.addEventListener("click", function handler() {
      renderPositions(positions, true);
      showAllBtn.classList.add("hidden");
      showAllBtn.removeEventListener("click", handler);
    });
  } else {
    showAllBtn.classList.add("hidden");
  }
}

function renderOtherFlights(flights) {
  if (!flights || flights.length === 0) return;
  document.getElementById("other-flights-section").classList.remove("hidden");
  const tbody = document.getElementById("other-body");
  tbody.innerHTML = "";
  flights.forEach(flight => {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    tr.addEventListener("click", () => { window.location.href = ROOT + "/flight/" + flight.id; });
    tr.innerHTML = `
      <td>${fmtTs(flight.first_seen)}</td>
      <td>${escHtml(flight.callsign) || "—"}</td>
      <td>${sourceBadge(flight.primary_source)}</td>
      <td>${fmtDur(flight.last_seen - flight.first_seen)}</td>
      <td>${(flight.total_positions || 0).toLocaleString()}</td>
    `;
    tbody.appendChild(tr);
  });
}

function _profileGridLines(width, height) {
  return [0.25, 0.5, 0.75].map(frac => {
    const yPos = (height - frac * height).toFixed(1);
    return `<line x1="0" x2="${width}" y1="${yPos}" y2="${yPos}" stroke="var(--border)" stroke-width="1.5"/>`;
  }).join("");
}

function renderProfile(positions) {
  const section = document.getElementById("profile-section");
  const altPts  = positions.filter(pos => pos.alt_baro != null);
  const spdPts  = positions.filter(pos => pos.gs       != null);
  const rssiPts = positions.filter(pos => pos.rssi     != null);

  const hasProfile = altPts.length >= 2 || spdPts.length >= 2;
  const hasRssi    = rssiPts.length >= 2;
  if (!hasProfile && !hasRssi) { section.classList.add("hidden"); return; }
  section.classList.remove("hidden");

  const SVG_W = 800, SVG_H = 100;
  const startTs = positions[0].ts, endTs = positions[positions.length - 1].ts;
  const duration = Math.max(endTs - startTs, 1);
  const xOf = ts => ((ts - startTs) / duration * SVG_W).toFixed(1);

  let html = "";

  if (hasProfile) {
    const maxAlt = Math.max(...altPts.map(pos => pos.alt_baro), 1);
    const maxSpd = Math.max(...spdPts.map(pos => pos.gs),       1);
    const altY   = alt => (SVG_H - alt / maxAlt * SVG_H).toFixed(1);
    const spdY   = spd => (SVG_H - spd / maxSpd * SVG_H).toFixed(1);
    const altLine = altPts.map(pos => `${xOf(pos.ts)},${altY(pos.alt_baro)}`).join(" ");
    const spdLine = spdPts.map(pos => `${xOf(pos.ts)},${spdY(pos.gs)}`).join(" ");

    html += `
      <div class="profile-wrap">
        <div class="profile-yaxis-left">
          <span>${altPts.length >= 2 ? fmtAlt(maxAlt) : ""}</span>
          <span>${altPts.length >= 2 ? "0" : ""}</span>
        </div>
        <div class="profile-area">
          <svg viewBox="0 0 ${SVG_W} ${SVG_H}" preserveAspectRatio="none">
            ${_profileGridLines(SVG_W, SVG_H)}
            ${altPts.length >= 2 ? `<polyline points="${altLine}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>` : ""}
            ${spdPts.length >= 2 ? `<polyline points="${spdLine}" fill="none" stroke="var(--green)"  stroke-width="2" stroke-linejoin="round"/>` : ""}
          </svg>
        </div>
        <div class="profile-yaxis-right">
          <span>${spdPts.length >= 2 ? fmtSpd(maxSpd) : ""}</span>
          <span>${spdPts.length >= 2 ? "0" : ""}</span>
        </div>
      </div>
      <div class="profile-legend-row">
        ${altPts.length >= 2 ? `<span class="profile-legend-item"><span class="profile-dot profile-dot-alt"></span> Altitude (${altLabel()})</span>` : ""}
        ${spdPts.length >= 2 ? `<span class="profile-legend-item"><span class="profile-dot profile-dot-spd"></span> Speed (${spdLabel()})</span>` : ""}
      </div>`;
  }

  if (hasRssi) {
    const minRssi  = Math.min(...rssiPts.map(pos => pos.rssi));
    const maxRssi  = Math.max(...rssiPts.map(pos => pos.rssi));
    const rssiSpan = Math.max(maxRssi - minRssi, 0.1);
    const rssiY    = val => (SVG_H - (val - minRssi) / rssiSpan * SVG_H).toFixed(1);
    const rssiLine = rssiPts.map(pos => `${xOf(pos.ts)},${rssiY(pos.rssi)}`).join(" ");

    html += `
      <div class="profile-wrap profile-wrap-rssi">
        <div class="profile-yaxis-left">
          <span>${maxRssi.toFixed(1)}</span>
          <span>${minRssi.toFixed(1)}</span>
        </div>
        <div class="profile-area">
          <svg viewBox="0 0 ${SVG_W} ${SVG_H}" preserveAspectRatio="none">
            ${_profileGridLines(SVG_W, SVG_H)}
            <polyline points="${rssiLine}" fill="none" stroke="var(--orange)" stroke-width="2" stroke-linejoin="round"/>
          </svg>
        </div>
        <div class="profile-yaxis-right" aria-hidden="true"></div>
      </div>
      <div class="profile-legend-row">
        <span class="profile-legend-item"><span class="profile-dot profile-dot-rssi"></span>
        <span title="Signal strength guide (readsb dBFS):&#10;0 to −3 dBFS → very strong&#10;−3 to −10 dBFS → strong&#10;−10 to −20 dBFS → moderate&#10;−20 to −30 dBFS → weak&#10;below −30 dBFS → at the edge of reception" class="rssi-help">RSSI (dBFS)</span></span>
      </div>`;
  }

  document.getElementById("profile-chart").innerHTML = html;
}

// loadPhoto provided by table-utils.js

async function load() {
  try {
    const resp = await fetch(ROOT + "/api/flights/" + FLIGHT_ID);
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    flightData   = data.flight;
    allPositions = data.positions;

    const flight = flightData;
    const titleParts = [flight.callsign, flight.registration, flight.aircraft_type].filter(Boolean);
    document.getElementById("flight-title").textContent =
      (titleParts.length ? titleParts.join(" / ") + " — " : "") + fmtTs(flight.first_seen);

    renderMeta(flight);
    renderPositions(allPositions, false);
    renderProfile(allPositions);
    renderOtherFlights(data.other_flights);

    const posWithLatLon = allPositions.filter(pos => pos.lat != null && pos.lon != null);
    initMap(posWithLatLon, flight, data.receiver_lat, data.receiver_lon);

    // Load photo asynchronously (don't block main render)
    loadPhoto(FLIGHT_ID, "photo-section");
  } catch (err) {
    document.getElementById("flight-title").textContent = "Error loading flight";
  }
}

// Re-render unit-dependent parts on units change
window.addEventListener("unitschange", () => {
  if (flightData) renderMeta(flightData);
  if (allPositions.length) {
    renderPositions(allPositions, allPositions.length <= PREVIEW_ROWS);
    renderProfile(allPositions);
  }
});

load();
