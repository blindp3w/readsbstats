/* live.js — currently tracked aircraft, auto-refreshes every 10 s */

const REFRESH_MS = 10000;

// ---------- Mini-map ----------

let liveMap = null;
let markerLayer = null;

function initMap(lat, lon) {
  liveMap = L.map("live-map", { zoomControl: true, attributionControl: false })
    .setView([lat, lon], 9);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 14,
  }).addTo(liveMap);
  L.circleMarker([lat, lon], { radius: 5, color: "var(--accent)", fillColor: "#4f8ef7", fillOpacity: 1, weight: 2 })
    .bindPopup("Receiver").addTo(liveMap);
  markerLayer = L.layerGroup().addTo(liveMap);
}

function updateMapMarkers(aircraft) {
  if (!markerLayer) return;
  markerLayer.clearLayers();
  const bounds = [];
  for (const ac of aircraft) {
    if (ac.lat == null || ac.lon == null) continue;
    const label = ac.callsign || ac.registration || ac.icao_hex;
    const marker = L.circleMarker([ac.lat, ac.lon], {
      radius: 5, weight: 1.5,
      color: "#fff", fillColor: "#22c55e", fillOpacity: .9,
    });
    marker.bindPopup(
      `<b>${escHtml(label)}</b><br>${escHtml(ac.aircraft_type || "")}`
      + `<br><a href="${ROOT}/flight/${ac.flight_id}">Details</a>`
    );
    marker.addTo(markerLayer);
    bounds.push([ac.lat, ac.lon]);
  }
}

function fmtSecondsAgo(sec) {
  if (sec < 60)  return sec + "s ago";
  if (sec < 3600) return Math.floor(sec / 60) + "m ago";
  return Math.floor(sec / 3600) + "h ago";
}

function seenClass(sec) {
  if (sec <= 15)  return "seen-fresh";
  if (sec <= 60)  return "seen-recent";
  return "seen-stale";
}

function renderLiveRow(aircraft) {
  const tr = document.createElement("tr");
  tr.className = "clickable";
  tr.addEventListener("click", () => { window.location.href = ROOT + "/flight/" + aircraft.flight_id; });

  const route = aircraft.origin_icao && aircraft.dest_icao
    ? escHtml(aircraft.origin_icao) + "→" + escHtml(aircraft.dest_icao)
    : "—";

  tr.innerHTML = `
    <td><a href="${ROOT}/aircraft/${encodeURIComponent(aircraft.icao_hex)}" class="stop-prop"><code>${escHtml(aircraft.icao_hex)}</code></a></td>
    <td>${escHtml(aircraft.callsign) || "—"}</td>
    <td>${route}</td>
    <td>${escHtml(aircraft.registration) || "—"}</td>
    <td>${escHtml(aircraft.aircraft_type) || "—"}</td>
    <td>${sourceBadge(aircraft.primary_source)}</td>
    <td class="${seenClass(aircraft.seconds_ago)}">${fmtSecondsAgo(aircraft.seconds_ago)}</td>
  `;
  tr.querySelector(".stop-prop").addEventListener("click", evt => evt.stopPropagation());
  return tr;
}

async function load() {
  try {
    const resp = await fetch(ROOT + "/api/live");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();

    if (!liveMap && data.receiver_lat != null) {
      initMap(data.receiver_lat, data.receiver_lon);
    }

    document.getElementById("live-count").textContent =
      "— " + data.count + " aircraft";
    document.getElementById("live-updated").textContent =
      "Updated " + new Date().toLocaleTimeString();

    const tbody = document.getElementById("live-body");
    if (data.aircraft.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="loading">No aircraft currently tracked.</td></tr>';
      updateMapMarkers([]);
      return;
    }

    tbody.innerHTML = "";
    data.aircraft.forEach(aircraft => tbody.appendChild(renderLiveRow(aircraft)));
    updateMapMarkers(data.aircraft);
  } catch (err) {
    document.getElementById("live-body").innerHTML =
      `<tr><td colspan="7" class="loading" style="color:var(--red)">Error: ${escHtml(err.message)}</td></tr>`;
  }
}

load();
let liveInterval = setInterval(load, REFRESH_MS);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearInterval(liveInterval);
    liveInterval = null;
  } else if (!liveInterval) {
    load();
    liveInterval = setInterval(load, REFRESH_MS);
  }
});
