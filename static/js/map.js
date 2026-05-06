"use strict";

/* map.js — full-screen aircraft map with live mode and historical rewind.
   Depends on: Leaflet (vendor), escHtml / ROOT / FLAG_MILITARY (base.html + table-utils.js)
   Constants injected by map.html: MAP_HISTORY_HOURS, RECEIVER_LAT, RECEIVER_LON */

const MAP_REFRESH_MS = 10000;   // live-mode poll interval
const PLAY_STEP_S    = 10;      // real seconds advanced per play tick at 1×

let mapInstance = null;
let markerLayer  = null;
let trailLayer   = null;

let aircraftMarkers = {};   // flight_id → { marker, trail }

let currentAt   = Math.floor(Date.now() / 1000);
let isLive      = true;
let isPlaying   = false;
let playSpeed   = 1;
let liveTimer   = null;
let playTimer   = null;
let debounceTimer = null;
let lastSnapshot  = null;

let heatLayer  = null;   // Leaflet.heat layer instance
let heatActive = false;
let heatWindow = "24h";

// DOM refs (set in init)
let sliderEl, timeDispEl, playBtn, modeLiveBtn, modeRewindBtn, jumpNowBtn, acCountEl;
let sidebarEl, sidebarToggleEl, sidebarBodyEl, sidebarCountEl;
let sidebarOpen = false;

// ---------- Map ----------

function initMap() {
  mapInstance = L.map("map-full", { preferCanvas: true });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors",
    maxZoom: 18,
  }).addTo(mapInstance);

  const lat = RECEIVER_LAT != null ? RECEIVER_LAT : 52.2;
  const lon = RECEIVER_LON != null ? RECEIVER_LON : 21.0;
  mapInstance.setView([lat, lon], 9);

  L.circleMarker([lat, lon], {
    radius: 5, color: "var(--accent)", fillColor: "#4f8ef7", fillOpacity: 1, weight: 2,
  }).bindPopup("Receiver").addTo(mapInstance);

  trailLayer  = L.layerGroup().addTo(mapInstance);
  markerLayer = L.layerGroup().addTo(mapInstance);
}

// ---------- Aircraft icon ----------

// Aircraft icon shapes sourced from tar1090 (wiedehopf/tar1090, MIT licence).
// Each entry: { viewBox, sw (stroke-width in coordinate space), path }
const ICON_SHAPES = {
  jet: {
    viewBox: '-1 -2 34 34',
    sw: 1.2,
    path: 'M16 1c-.17 0-.67.58-.9 1.03-.6 1.21-.6 1.15-.65 5.2-.04 2.97-.08 3.77-.18 3.9-.15.17-1.82 1.1-1.98 1.1-.08 0-.1-.25-.05-.83.03-.5.01-.92-.05-1.08-.1-.25-.13-.26-.71-.26-.82 0-.86.07-.78 1.5.03.6.08 1.17.11 1.25.05.12-.02.2-.25.33l-8 4.2c-.2.2-.18.1-.19 1.29 3.9-1.2 3.71-1.21 3.93-1.21.06 0 .1 0 .13.14.08.3.28.3.28-.04 0-.25.03-.27 1.16-.6.65-.2 1.22-.35 1.28-.35.05 0 .12.04.15.17.07.3.27.27.27-.08 0-.25.01-.27.7-.47.68-.1.98-.09 1.47-.1.18 0 .22 0 .26.18.06.34.22.35.27-.01.04-.2.1-.17 1.06-.14l1.07.02.05 4.2c.05 3.84.07 4.28.26 5.09.11.49.2.99.2 1.11 0 .19-.31.43-1.93 1.5l-1.93 1.26v1.02l4.13-.95.63 1.54c.05.07.12.09.19.09s.14-.02.19-.09l.63-1.54 4.13.95V29.3l-1.93-1.27c-1.62-1.06-1.93-1.3-1.93-1.49 0-.12.09-.62.2-1.11.19-.81.2-1.25.26-5.09l.05-4.2 1.07-.02c.96-.03 1.02-.05 1.06.14.05.36.21.35.27 0 .04-.17.08-.16.26-.16.49 0 .8-.02 1.48.1.68.2.69.21.69.46 0 .35.2.38.27.08.03-.13.1-.17.15-.17.06 0 .63.15 1.28.34 1.13.34 1.16.36 1.16.61 0 .35.2.34.28.04.03-.13.07-.14.13-.14.22 0 .03 0 3.93 1.2-.01-1.18.02-1.07-.19-1.27l-8-4.21c-.23-.12-.3-.21-.25-.33.03-.08.08-.65.11-1.25.08-1.43.04-1.5-.78-1.5-.58 0-.61.01-.71.26-.06.16-.08.58-.05 1.08.04.58.03.83-.05.83-.16 0-1.83-.93-1.98-1.1-.1-.13-.14-.93-.18-3.9-.05-4.05-.05-3.99-.65-5.2C16.67 1.58 16.17 1 16 1z',
  },
  light: {
    viewBox: '0 -1 32 31',
    sw: 1.2,
    path: 'M16.36 20.96l2.57.27s.44.05.4.54l-.02.63s-.03.47-.45.54l-2.31.34-.44-.74-.22 1.63-.25-1.62-.38.73-2.35-.35s-.44-.1-.43-.6l-.02-.6s0-.5.48-.5l2.5-.27-.56-5.4-3.64-.1-5.83-1.02h-.45v-2.06s-.07-.37.46-.34l5.8-.17 3.55.12s-.1-2.52.52-2.82l-1.68-.04s-.1-.06 0-.14l1.94-.03s.35-1.18.7 0l1.91.04s.11.05 0 .14l-1.7.02s.62-.09.56 2.82l3.54-.1 5.81.17s.51-.04.48.35l-.01 2.06h-.47l-5.8 1-3.67.11z',
  },
  heli: {
    viewBox: '-13 -13 90 90',
    sw: 3.0,
    path: 'm 24.698,60.712 c 0,0 -0.450,2.134 -0.861,2.142 -0.561,0.011 -0.480,-3.836 -0.593,-5.761 -0.064,-1.098 1.381,-1.192 1.481,-0.042 l 5.464,0.007 -0.068,-9.482 -0.104,-1.108 c -2.410,-2.131 -3.028,-3.449 -3.152,-7.083 l -12.460,13.179 c -0.773,0.813 -2.977,0.599 -3.483,-0.428 L 26.920,35.416 26.866,29.159 11.471,14.513 c -0.813,-0.773 -0.599,-2.977 0.428,-3.483 l 14.971,14.428 0.150,-5.614 c -0.042,-1.324 1.075,-4.784 3.391,-5.633 0.686,-0.251 2.131,-0.293 3.033,0.008 2.349,0.783 3.433,4.309 3.391,5.633 l 0.073,4.400 12.573,-12.763 c 0.779,-0.807 2.977,-0.599 3.483,0.428 L 37.054,28.325 37.027,35.027 52.411,49.365 c 0.813,0.773 0.599,2.977 -0.428,3.483 L 36.992,38.359 c -0.124,3.634 -0.742,5.987 -3.152,8.118 l -0.104,1.108 -0.068,9.482 5.321,-0.068 c 0.101,-1.150 1.546,-1.057 1.481,0.042 -0.113,1.925 -0.032,5.772 -0.593,5.761 -0.412,-0.008 -0.861,-2.142 -0.861,-2.142 l -5.387,-0.011 0.085,9.377 -1.094,2.059 -1.386,-0.018 -1.093,-2.049 0.085,-9.377 z',
  },
  glider: {
    viewBox: '-5.8 -10 76 76',
    sw: 2.4,
    path: 'm 32.000,45.932 -0.215,0.314 c -0.118,0.173 -0.196,0.239 -0.378,0.401 -0.226,0.145 -0.850,-0.045 -1.196,-0.137 -0.658,-0.204 -1.909,-0.478 -2.984,-0.718 -0.065,-0.021 -0.186,-0.136 -0.406,-0.344 -0.342,-0.323 -0.409,-0.463 -0.459,-0.961 -0.074,-0.730 0.183,-1.127 0.795,-1.228 0.218,-0.036 0.732,-0.130 1.143,-0.210 0.411,-0.080 1.132,-0.201 1.602,-0.271 1.252,-0.185 1.635,-0.299 1.701,-0.507 0.059,-0.186 -0.006,-2.549 -0.101,-3.654 -0.110,-2.092 -0.181,-3.601 -0.281,-5.738 0.039,-0.214 -0.274,-0.732 -0.553,-0.915 l -5.180,-0.560 c -0.611,-0.069 -3.989,-0.350 -5.732,-0.476 -1.476,-0.108 -2.940,-0.246 -4.432,-0.362 l -3.097,-0.439 C 7.935,29.593 4.497,29.014 2.499e-5,28.410 l 0.019,-2.401 5.562,-0.286 c 2.699,-0.023 6.207,-0.092 9.264,-0.183 0.646,-0.019 4.548,-0.040 8.671,-0.047 l 7.496,-0.012 -0.017,-2.376 c -0.007,-1.423 -0.104,-3.049 0.253,-4.827 0.028,-0.088 0.121,-0.396 0.344,-0.722 0.071,-0.090 0.213,-0.175 0.408,-0.255 0.195,0.080 0.337,0.165 0.408,0.255 0.223,0.325 0.316,0.633 0.343,0.722 0.357,1.778 0.261,3.405 0.253,4.827 l -0.016,2.376 7.496,0.012 c 4.123,0.007 8.025,0.028 8.671,0.047 3.057,0.091 6.564,0.160 9.264,0.183 l 5.562,0.286 0.019,2.401 c -4.497,0.605 -7.935,1.183 -12.228,1.717 l -3.097,0.439 c -1.492,0.116 -2.956,0.254 -4.432,0.362 -1.743,0.127 -5.121,0.408 -5.732,0.476 l -5.180,0.560 c -0.278,0.182 -0.592,0.701 -0.553,0.915 -0.100,2.136 -0.171,3.646 -0.281,5.738 -0.095,1.105 -0.160,3.468 -0.101,3.654 0.066,0.208 0.449,0.322 1.701,0.507 0.470,0.069 1.191,0.191 1.602,0.271 0.411,0.080 0.926,0.174 1.143,0.210 0.612,0.101 0.870,0.498 0.795,1.228 -0.051,0.499 -0.118,0.638 -0.460,0.961 -0.220,0.208 -0.341,0.323 -0.406,0.344 -1.075,0.240 -2.326,0.513 -2.984,0.718 -0.346,0.091 -0.970,0.282 -1.196,0.137 -0.182,-0.162 -0.260,-0.228 -0.378,-0.401 z',
  },
};

// Map ADS-B category codes to icon types.
// Category field: A1=Light, A2=Small, A3=Large, A4=757, A5=Heavy, A7=Rotorcraft, B1=Glider, B4=Ultralight
const CATEGORY_MAP = {
  A1: "light",
  A2: "jet",  A3: "jet", A4: "jet", A5: "jet",
  A7: "heli",
  B1: "glider", B4: "light",
};

// Type-code regexes for fallback when category is absent
const HELI_REGEX  = /^(EC[0-9]|AS[0-9]|R[0-9][0-9]|B[0-9][0-9][0-9]B|A[0-9][0-9][0-9]N|S[0-9][0-9]|H[0-9][0-9][0-9]|AW[0-9]|AB[0-9])/i;
const LIGHT_REGEX = /^(C1[0-9][0-9]|C17[0-9]|P28|BE3[0-9]|SR2[0-9]|DA4[0-9]|TB2[0-9]|PA[0-9]|AA[0-9]|DR[0-9]|RV[0-9])/i;

function getIconType(ac) {
  if (ac.category && CATEGORY_MAP[ac.category]) {
    return CATEGORY_MAP[ac.category];
  }
  const t = (ac.aircraft_type || "").toUpperCase();
  if (t && HELI_REGEX.test(t))  return "heli";
  if (t && LIGHT_REGEX.test(t)) return "light";
  return "jet";
}

function aircraftIcon(track, flags, iconType) {
  const isMilitary = (flags & FLAG_MILITARY) !== 0;
  // White fill with colored stroke — visible on any OSM tile background
  const fill   = isMilitary ? "#ef4444" : "#ffffff";
  const stroke = isMilitary ? "#7f1d1d" : "#1d4ed8";
  const deg    = track != null ? track : 0;
  const shape  = ICON_SHAPES[iconType] || ICON_SHAPES.jet;
  return L.divIcon({
    className: "ac-icon-wrap",
    html: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${shape.viewBox}" width="30" height="30"
            style="transform:rotate(${deg}deg);transform-origin:center;display:block;filter:drop-shadow(0 1px 3px rgba(0,0,0,.55))">
      <path d="${shape.path}" fill="${fill}" stroke="${stroke}" stroke-width="${shape.sw}" stroke-linejoin="round"/>
    </svg>`,
    iconSize: [30, 30],
    iconAnchor: [15, 15],
    popupAnchor: [0, -18],
  });
}

// ---------- Sidebar ----------

function fmtSecondsAgo(sec) {
  if (sec < 60)   return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m";
  return Math.floor(sec / 3600) + "h";
}

function seenClass(sec) {
  if (sec <= 15) return "seen-fresh";
  if (sec <= 60) return "seen-recent";
  return "seen-stale";
}

function renderSidebar(aircraft) {
  if (!sidebarBodyEl) return;
  const u    = getUnits();
  const frag = document.createDocumentFragment();
  const sorted = [...aircraft].sort((a, b) => (a.seconds_ago ?? 0) - (b.seconds_ago ?? 0));

  for (const ac of sorted) {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    tr.addEventListener("click", () => { window.location.href = ROOT + "/flight/" + ac.flight_id; });

    const route = (ac.origin_icao && ac.dest_icao)
      ? escHtml(ac.origin_icao) + "→" + escHtml(ac.dest_icao) : "—";
    const alt  = ac.alt_baro != null ? fmtAlt(ac.alt_baro, u) : "—";
    const spd  = ac.gs       != null ? fmtSpd(ac.gs, u)       : "—";
    const sec  = ac.seconds_ago ?? null;
    const seen = sec != null ? fmtSecondsAgo(sec) : "—";
    const cls  = sec != null ? seenClass(sec)      : "";

    tr.innerHTML = `
      <td><a href="${ROOT}/aircraft/${encodeURIComponent(ac.icao_hex)}" class="stop-prop"><code>${escHtml(ac.icao_hex)}</code></a></td>
      <td>${escHtml(ac.callsign) || "—"}</td>
      <td>${route}</td>
      <td>${escHtml(ac.registration) || "—"}</td>
      <td>${escHtml(ac.aircraft_type) || "—"}</td>
      <td>${alt}</td>
      <td>${spd}</td>
      <td>${sourceBadge(ac.primary_source)}</td>
      <td class="${cls}">${seen}</td>`;
    tr.querySelector(".stop-prop").addEventListener("click", evt => evt.stopPropagation());
    frag.appendChild(tr);
  }

  sidebarBodyEl.innerHTML = "";
  sidebarBodyEl.appendChild(frag);
  if (sidebarCountEl) sidebarCountEl.textContent = aircraft.length + " aircraft";
}

function toggleSidebar() {
  sidebarOpen = !sidebarOpen;
  sidebarEl.classList.toggle("open", sidebarOpen);
  sidebarToggleEl.textContent = sidebarOpen ? "▶" : "◀";
  sidebarToggleEl.title = sidebarOpen ? "Hide aircraft list" : "Show aircraft list";
}

// ---------- Popup ----------

function buildPopup(ac) {
  const label = escHtml(ac.callsign || ac.registration || ac.icao_hex);
  const reg   = (ac.callsign && ac.registration) ? " · " + escHtml(ac.registration) : "";
  const type  = escHtml(ac.aircraft_type || "—");
  const u     = getUnits();
  const alt   = ac.alt_baro != null ? fmtAlt(ac.alt_baro, u) : "—";
  const spd   = ac.gs      != null ? fmtSpd(ac.gs, u)      : "—";
  return `<b>${label}</b>${reg}<br>${type}<br>${alt} · ${spd}`
       + `<br>${sourceBadge(ac.primary_source)}`
       + `<br><a href="${ROOT}/flight/${ac.flight_id}">→ Flight detail</a>`;
}

// ---------- Render ----------

function renderSnapshot(data) {
  const seen = new Set();

  for (const ac of data.aircraft) {
    if (ac.lat == null || ac.lon == null) continue;
    const fid = ac.flight_id;
    seen.add(fid);

    const icon = aircraftIcon(ac.track, ac.flags, getIconType(ac));
    const popup = buildPopup(ac);
    const trailCoords = (ac.trail || []).map(pt => [pt[0], pt[1]]);

    if (aircraftMarkers[fid]) {
      const entry = aircraftMarkers[fid];
      entry.marker.setLatLng([ac.lat, ac.lon]);
      entry.marker.setIcon(icon);
      entry.marker.setPopupContent(popup);
      entry.trail.setLatLngs(trailCoords);
    } else {
      const marker = L.marker([ac.lat, ac.lon], { icon });
      marker.bindPopup(popup);
      marker.addTo(markerLayer);

      const trail = L.polyline(trailCoords, {
        color: "#4f8ef7", weight: 1.5, opacity: 0.55,
      }).addTo(trailLayer);

      aircraftMarkers[fid] = { marker, trail };
    }
  }

  // Remove aircraft that left the snapshot
  for (const fid of Object.keys(aircraftMarkers)) {
    if (!seen.has(parseInt(fid, 10))) {
      markerLayer.removeLayer(aircraftMarkers[fid].marker);
      trailLayer.removeLayer(aircraftMarkers[fid].trail);
      delete aircraftMarkers[fid];
    }
  }

  const count = seen.size;
  if (acCountEl) acCountEl.textContent = count + " aircraft";

  // Keep the nav badge in sync with what's actually on the map
  const badge = document.getElementById("live-badge");
  if (badge) {
    badge.textContent = count + " live";
    badge.classList.toggle("hidden", count === 0);
  }

  renderSidebar(data.aircraft);
}

// ---------- Fetch ----------

async function fetchSnapshot(at) {
  const url = at == null
    ? ROOT + "/api/map/snapshot?trail=10"
    : ROOT + "/api/map/snapshot?at=" + at + "&trail=10";
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return await resp.json();
  } catch (err) {
    console.error("map snapshot error:", err.message);
    return null;
  }
}

async function refresh() {
  const data = await fetchSnapshot(isLive ? null : currentAt);
  if (data) { lastSnapshot = data; renderSnapshot(data); }
}

// ---------- Time display ----------

function updateTimeDisp() {
  if (!timeDispEl) return;
  const d = new Date(currentAt * 1000);
  timeDispEl.textContent = d.toLocaleString(undefined, {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false,
  });
}

// ---------- Playback ----------

function stopPlayback() {
  isPlaying = false;
  if (playBtn) playBtn.textContent = "▶";
  clearInterval(playTimer);
  playTimer = null;
}

function startPlayback() {
  if (isLive || isPlaying) return;
  isPlaying = true;
  if (playBtn) playBtn.textContent = "⏸";

  const intervalMs = Math.max(250, MAP_REFRESH_MS / playSpeed);
  playTimer = setInterval(() => {
    const nowTs = Math.floor(Date.now() / 1000);
    currentAt = Math.min(currentAt + PLAY_STEP_S * playSpeed, nowTs);
    if (sliderEl) sliderEl.value = currentAt;
    updateTimeDisp();
    if (currentAt >= nowTs) {
      setLiveMode();
    } else {
      refresh();
    }
  }, intervalMs);
}

function togglePlayback() {
  if (isLive) return;
  if (isPlaying) stopPlayback();
  else startPlayback();
}

// ---------- Mode ----------

function setLiveMode() {
  isLive = true;
  stopPlayback();
  currentAt = Math.floor(Date.now() / 1000);
  if (sliderEl) { sliderEl.value = currentAt; sliderEl.disabled = true; }
  if (modeLiveBtn)   modeLiveBtn.classList.add("active");
  if (modeRewindBtn) modeRewindBtn.classList.remove("active");
  updateTimeDisp();
  refresh();
}

function setRewindMode() {
  if (!isLive) return;
  isLive = false;
  currentAt = Math.floor(Date.now() / 1000) - 3600;
  if (sliderEl) { sliderEl.value = currentAt; sliderEl.disabled = false; }
  if (modeLiveBtn)   modeLiveBtn.classList.remove("active");
  if (modeRewindBtn) modeRewindBtn.classList.add("active");
  updateTimeDisp();
  refresh();
}

// ---------- Live polling ----------

function startLivePolling() {
  if (liveTimer) return;
  liveTimer = setInterval(() => {
    if (!isLive) return;
    currentAt = Math.floor(Date.now() / 1000);
    updateTimeDisp();
    refresh();
  }, MAP_REFRESH_MS);
}

// ---------- Init ----------

// ---------- Position density heatmap ----------

async function loadHeatmap(win) {
  const statusEl = document.getElementById("map-heat-status");
  if (statusEl) { statusEl.textContent = "Loading…"; statusEl.classList.remove("hidden"); }
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), 55000); // 55s < nginx 60s
  try {
    const resp = await fetch(ROOT + "/api/map/heatmap?window=" + encodeURIComponent(win),
                             { signal: ctrl.signal });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    // Guard: user may have toggled the heatmap off while this fetch was in-flight.
    if (!heatActive) return;
    if (heatLayer) { mapInstance.removeLayer(heatLayer); heatLayer = null; }
    if (data.points.length > 0) {
      heatLayer = L.heatLayer(data.points, {
        radius:     20,
        blur:       18,
        maxZoom:    13,
        max:        1.0,
        minOpacity: 0.35,
        gradient:   { 0.2: "#ffd700", 0.5: "#ff7700", 0.8: "#e62200", 1.0: "#9b0000" },
      }).addTo(mapInstance);
    }
    if (statusEl) statusEl.classList.add("hidden");
  } catch (err) {
    console.error("Heatmap load failed:", err);
    if (statusEl) {
      statusEl.textContent = err.name === "AbortError" ? "Timed out — try a shorter window" : "Failed to load";
      statusEl.classList.remove("hidden");
    }
  } finally {
    clearTimeout(timeout);
  }
}

async function toggleHeatmap() {
  const btn    = document.getElementById("map-heat-btn");
  const winSel = document.getElementById("map-heat-windows");
  if (heatActive) {
    if (heatLayer) { mapInstance.removeLayer(heatLayer); heatLayer = null; }
    heatActive = false;
    if (btn)    btn.classList.remove("active");
    if (winSel) winSel.classList.add("hidden");
    const statusEl = document.getElementById("map-heat-status");
    if (statusEl) statusEl.classList.add("hidden");
  } else {
    heatActive = true;
    if (btn)    btn.classList.add("active");
    if (winSel) winSel.classList.remove("hidden");
    await loadHeatmap(heatWindow);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initMap();

  sliderEl        = document.getElementById("map-slider");
  timeDispEl      = document.getElementById("map-time-disp");
  playBtn         = document.getElementById("map-play-btn");
  modeLiveBtn     = document.getElementById("map-mode-live");
  modeRewindBtn   = document.getElementById("map-mode-rewind");
  jumpNowBtn      = document.getElementById("map-jump-now");
  acCountEl       = document.getElementById("map-ac-count");
  sidebarEl       = document.getElementById("map-sidebar");
  sidebarToggleEl = document.getElementById("map-sidebar-toggle");
  sidebarBodyEl   = document.getElementById("map-sidebar-body");
  sidebarCountEl  = document.getElementById("map-sidebar-count");

  if (sidebarToggleEl) sidebarToggleEl.addEventListener("click", toggleSidebar);

  const nowTs      = Math.floor(Date.now() / 1000);
  const sliderMin  = nowTs - MAP_HISTORY_HOURS * 3600;

  if (sliderEl) {
    sliderEl.min   = sliderMin;
    sliderEl.max   = nowTs;
    sliderEl.step  = 30;
    sliderEl.value = nowTs;

    sliderEl.addEventListener("input", () => {
      if (isLive) return;
      currentAt = parseInt(sliderEl.value, 10);
      updateTimeDisp();
      stopPlayback();
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(refresh, 300);
    });
  }

  if (playBtn)       playBtn.addEventListener("click", togglePlayback);
  if (modeLiveBtn)   modeLiveBtn.addEventListener("click", setLiveMode);
  if (modeRewindBtn) modeRewindBtn.addEventListener("click", setRewindMode);
  if (jumpNowBtn)    jumpNowBtn.addEventListener("click", setLiveMode);

  document.querySelectorAll(".map-speed-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".map-speed-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const wasPlaying = isPlaying;
      stopPlayback();
      playSpeed = parseInt(btn.dataset.speed, 10);
      if (wasPlaying) startPlayback();
    });
  });

  document.querySelectorAll("[data-skip]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (isLive) return;
      const nowTs2 = Math.floor(Date.now() / 1000);
      const min = parseInt(sliderEl ? sliderEl.min : nowTs2 - MAP_HISTORY_HOURS * 3600, 10);
      currentAt = Math.max(min, Math.min(currentAt + parseInt(btn.dataset.skip, 10), nowTs2));
      if (sliderEl) sliderEl.value = currentAt;
      updateTimeDisp();
      stopPlayback();
      refresh();
    });
  });

  // Heatmap toggle + window selector
  const heatBtn = document.getElementById("map-heat-btn");
  if (heatBtn) heatBtn.addEventListener("click", toggleHeatmap);
  document.querySelectorAll(".map-heat-win").forEach(btn => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll(".map-heat-win").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      heatWindow = btn.dataset.win;
      if (heatActive) await loadHeatmap(heatWindow);
    });
  });

  // Initial load
  currentAt = nowTs;
  updateTimeDisp();
  refresh();
  startLivePolling();

  document.addEventListener("unitschange", () => {
    if (lastSnapshot) renderSnapshot(lastSnapshot);
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearInterval(liveTimer);
      liveTimer = null;
      stopPlayback();
    } else if (!liveTimer) {
      if (isLive) refresh();
      startLivePolling();
    }
  });
});
