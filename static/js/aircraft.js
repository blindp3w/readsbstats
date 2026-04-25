/* aircraft.js — single aircraft detail page */

const PAGE_SIZE = 100;
let currentOffset    = 0;
let currentTotal     = 0;
let lastData         = [];
let currentSort      = { col: "first_seen", dir: "desc" };
let loadController   = null;

function buildUrl(offset) {
  const query = new URLSearchParams({
    limit:    PAGE_SIZE,
    offset,
    sort_by:  currentSort.col,
    sort_dir: currentSort.dir,
  });
  return ROOT + "/api/aircraft/" + ICAO_HEX + "/flights?" + query;
}

function initSort() {
  initSortHeaders("flights-table", currentSort, () => load(0));
}

function renderRow(flight) {
  const tr = document.createElement("tr");
  tr.className = "clickable";
  tr.addEventListener("click", () => { window.location.href = ROOT + "/flight/" + flight.id; });
  tr.innerHTML = `
    <td>${fmtTs(flight.first_seen)}</td>
    <td>${escHtml(flight.callsign) || "—"}</td>
    <td>${sourceBadge(flight.primary_source)}</td>
    <td>${fmtDur(flight.duration_sec)}</td>
    <td>${fmtAlt(flight.max_alt_baro)}</td>
    <td>${fmtSpd(flight.max_gs)}</td>
    <td>${flight.max_distance_nm != null ? fmtDist(flight.max_distance_nm) : "—"}</td>
    <td>${(flight.total_positions || 0).toLocaleString()}</td>
  `;
  return tr;
}

function updateColumnHeaders() {
  const thead = document.querySelector("#flights-table thead tr");
  if (!thead) return;
  thead.cells[4].textContent = "Max Alt";
  thead.cells[5].textContent = "Max Spd";
  thead.cells[6].textContent = "Max Dist";
}

function renderTable() {
  const tbody = document.getElementById("flights-body");
  tbody.innerHTML = "";
  if (lastData.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="loading">No flights found.</td></tr>';
    return;
  }
  const frag = document.createDocumentFragment();
  lastData.forEach(flight => frag.appendChild(renderRow(flight)));
  tbody.appendChild(frag);
  updateColumnHeaders();
}

function renderAircraftPagination(total, offset) {
  renderPagination("pagination", total, PAGE_SIZE, offset, o => load(o));
}

function renderInfo(info) {
  const reg  = info.registration || null;
  const type = info.type_code    || null;
  const desc = info.type_desc    || null;

  // Page title
  const titleParts = [reg, type, desc].filter(Boolean);
  if (titleParts.length) {
    document.getElementById("aircraft-title").textContent = ICAO_HEX + " — " + titleParts.join(" / ");
  }

  // Flag badge next to title
  const badge = flagBadge(info.flags, "medium");
  if (badge) document.getElementById("aircraft-title").insertAdjacentHTML("beforeend", badge);

  // Summary cards
  document.getElementById("a-flights").textContent = (info.total_flights || 0).toLocaleString();
  document.getElementById("a-time").textContent    = fmtDur(info.total_duration_sec || 0);
  document.getElementById("a-first").textContent   = info.first_seen
    ? new Date(info.first_seen * 1000).toLocaleDateString() : "—";
  document.getElementById("a-last").textContent    = info.last_seen
    ? new Date(info.last_seen  * 1000).toLocaleDateString() : "—";
  document.getElementById("a-country").textContent = info.country || "Unknown";
}

// loadPhoto provided by table-utils.js

async function load(offset) {
  currentOffset = offset;

  if (loadController) loadController.abort();
  loadController = new AbortController();

  const tbody = document.getElementById("flights-body");
  tbody.innerHTML = '<tr><td colspan="8" class="loading">Loading…</td></tr>';
  document.getElementById("pagination").innerHTML = "";

  try {
    const resp = await fetch(buildUrl(offset), { signal: loadController.signal });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();

    currentTotal = data.total;
    lastData     = data.flights;

    if (offset === 0 && data.aircraft_info) {
      renderInfo(data.aircraft_info);
      initWatchButton(data.aircraft_info.registration);
      // Load photo using the most recent flight's ID
      if (data.flights.length > 0) loadPhoto(data.flights[0].id, "photo-section");
    }

    renderTable();
    renderAircraftPagination(data.total, offset);
  } catch (err) {
    if (err.name === "AbortError") return;
    tbody.innerHTML = `<tr><td colspan="8" class="loading" style="color:var(--red)">Error: ${escHtml(err.message)} <button class="btn-retry">Retry</button></td></tr>`;
    tbody.querySelector(".btn-retry").addEventListener("click", () => load(currentOffset));
  }
}

window.addEventListener("unitschange", () => renderTable());

// ---------------------------------------------------------------------------
// Watch button
// ---------------------------------------------------------------------------

async function initWatchButton(registration) {
  const watchBtn = document.getElementById("watch-btn");
  if (!watchBtn) return;

  let isWatching = false;
  try {
    const resp = await fetch(ROOT + "/api/watchlist");
    if (resp.ok) {
      const data = await resp.json();
      isWatching = data.entries.some(
        entry => entry.match_type === "icao" && entry.value === ICAO_HEX.toLowerCase()
      );
    }
  } catch (err) { console.error("initWatchButton:", err); }

  function render() {
    watchBtn.textContent = isWatching ? "Watching ✓" : "Watch";
    watchBtn.disabled    = isWatching;
    watchBtn.classList.remove("hidden");
  }
  render();

  watchBtn.addEventListener("click", async () => {
    try {
      const resp = await fetch(ROOT + "/api/watchlist", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          match_type: "icao",
          value:      ICAO_HEX.toLowerCase(),
          label:      registration || null,
        }),
      });
      if (resp.ok || resp.status === 409) { isWatching = true; render(); }
    } catch (err) { console.error("watchlist add:", err); }
  });
}

// Back link: use browser history when there is a previous page on our site
(function() {
  const link = document.getElementById("back-link");
  if (document.referrer && new URL(document.referrer).origin === location.origin) {
    link.addEventListener("click", function(e) {
      e.preventDefault();
      history.back();
    });
  }
})();

initSort();
load(0);
