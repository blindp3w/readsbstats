/* flights.js — flight list page */

const PAGE_SIZE = 100;
let currentOffset    = 0;
let currentTotal     = 0;
let currentParams    = {};
let lastData         = [];   // cached for re-render on units change
let currentSort      = { col: "first_seen", dir: "desc" };
let flightsController = null;

function getFormParams() {
  const form = document.getElementById("search-form");
  const params = {};
  ["callsign", "icao", "registration", "aircraft_type", "date", "source", "flags", "squawk"].forEach(key => {
    const input = form.querySelector(`[name="${key}"]`);
    if (input && input.value.trim()) params[key] = input.value.trim();
  });
  return params;
}

function buildUrl(params, offset) {
  const query = new URLSearchParams({
    ...params,
    limit: PAGE_SIZE,
    offset,
    sort_by:  currentSort.col,
    sort_dir: currentSort.dir,
  });
  return ROOT + "/api/flights?" + query;
}

function initSort() {
  initSortHeaders("flights-table", currentSort, () => loadFlights(currentParams, 0));
}

function fmtRoute(flight) {
  if (flight.origin_icao && flight.dest_icao)
    return escHtml(flight.origin_icao) + "→" + escHtml(flight.dest_icao);
  if (flight.origin_icao) return escHtml(flight.origin_icao) + "→?";
  if (flight.dest_icao)   return "?→" + escHtml(flight.dest_icao);
  return "—";
}

function renderRow(flight) {
  const tr = document.createElement("tr");
  tr.className = "clickable";
  tr.addEventListener("click", () => { window.location.href = ROOT + "/flight/" + flight.id; });
  const milBadge = flagBadge(flight.flags, "short");
  tr.innerHTML = `
    <td>${fmtTs(flight.first_seen)}</td>
    <td><a href="${ROOT}/aircraft/${encodeURIComponent(flight.icao_hex)}" class="stop-prop"><code>${escHtml(flight.icao_hex)}</code></a></td>
    <td>${escHtml(flight.callsign) || "—"}</td>
    <td>${fmtRoute(flight)}</td>
    <td>${escHtml(flight.registration) || "—"}</td>
    <td title="${escHtml(flight.type_desc) || ""}">${escHtml(flight.aircraft_type) || "—"}${milBadge}</td>
    <td>${sourceBadge(flight.primary_source)}</td>
    <td>${fmtDur(flight.duration_sec)}</td>
    <td>${fmtAlt(flight.max_alt_baro)}</td>
    <td>${fmtSpd(flight.max_gs)}</td>
    <td>${flight.max_distance_nm != null ? fmtDist(flight.max_distance_nm) : "—"}</td>
    <td>${(flight.total_positions || 0).toLocaleString()}</td>
  `;
  tr.querySelector(".stop-prop").addEventListener("click", evt => evt.stopPropagation());
  return tr;
}

function updateColumnHeaders() {
  const thead = document.querySelector("#flights-table thead tr");
  if (!thead) return;
  thead.cells[8].textContent = "Max Alt";
  thead.cells[9].textContent = "Max Spd";
  thead.cells[10].textContent = "Max Dist";
}

function renderTable() {
  const tbody = document.getElementById("flights-body");
  tbody.innerHTML = "";
  if (lastData.length === 0) {
    tbody.innerHTML = '<tr><td colspan="12" class="loading">No flights found.</td></tr>';
    return;
  }
  const frag = document.createDocumentFragment();
  lastData.forEach(flight => frag.appendChild(renderRow(flight)));
  tbody.appendChild(frag);
  updateColumnHeaders();
}

function renderFlightsPagination(total, offset) {
  renderPagination("pagination", total, PAGE_SIZE, offset, o => loadFlights(currentParams, o));
}

async function loadFlights(params, offset) {
  currentParams = params;
  currentOffset = offset;

  if (flightsController) flightsController.abort();
  flightsController = new AbortController();

  const tbody = document.getElementById("flights-body");
  tbody.innerHTML = '<tr><td colspan="12" class="loading">Loading…</td></tr>';
  document.getElementById("pagination").innerHTML = "";

  try {
    const resp = await fetch(buildUrl(params, offset), { signal: flightsController.signal });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();

    currentTotal = data.total;
    lastData = data.flights;

    document.getElementById("result-info").textContent =
      `${data.total.toLocaleString()} flight${data.total !== 1 ? "s" : ""} found` +
      (offset > 0 ? ` — showing ${offset + 1}–${Math.min(offset + PAGE_SIZE, data.total)}` : "");

    renderTable();
    renderFlightsPagination(data.total, offset);
  } catch (err) {
    if (err.name === "AbortError") return;
    tbody.innerHTML = `<tr><td colspan="12" class="loading" style="color:var(--red)">Error: ${escHtml(err.message)} <button class="btn-retry">Retry</button></td></tr>`;
    tbody.querySelector(".btn-retry").addEventListener("click", () => loadFlights(currentParams, currentOffset));
  }
}

// Re-render on unit change without re-fetching
window.addEventListener("unitschange", () => renderTable());

// Restore params from URL on load
function paramsFromUrl() {
  const query = new URLSearchParams(location.search);
  const params = {};
  ["callsign", "icao", "registration", "aircraft_type", "date", "source", "flags", "squawk"].forEach(key => {
    if (query.has(key)) params[key] = query.get(key);
  });
  return params;
}

function applyParamsToForm(params) {
  const form = document.getElementById("search-form");
  Object.entries(params).forEach(([key, val]) => {
    const input = form.querySelector(`[name="${key}"]`);
    if (input) input.value = val;
  });
}

// Collapsible filters on mobile
const filtersToggle = document.getElementById("filters-toggle");
const searchFields = document.getElementById("search-fields");
if (filtersToggle && searchFields) {
  // Auto-open if URL has active filters
  const initParams = paramsFromUrl();
  if (Object.keys(initParams).length > 0) searchFields.classList.add("open");
  filtersToggle.addEventListener("click", () => {
    const open = searchFields.classList.toggle("open");
    filtersToggle.textContent = open ? "Filters ▴" : "Filters ▾";
  });
}

document.getElementById("search-form").addEventListener("submit", evt => {
  evt.preventDefault();
  const params = getFormParams();
  const query = new URLSearchParams(params);
  history.pushState({}, "", location.pathname + (query.toString() ? "?" + query : ""));
  loadFlights(params, 0);
});

document.getElementById("clear-btn").addEventListener("click", () => {
  document.getElementById("search-form").reset();
  history.pushState({}, "", location.pathname);
  loadFlights({}, 0);
});

document.getElementById("export-btn").addEventListener("click", () => {
  const query = new URLSearchParams({
    ...currentParams,
    sort_by:  currentSort.col,
    sort_dir: currentSort.dir,
  });
  window.location.href = ROOT + "/api/flights/export.csv?" + query;
});

window.addEventListener("popstate", () => {
  const params = paramsFromUrl();
  applyParamsToForm(params);
  loadFlights(params, 0);
});

const initParams = paramsFromUrl();
applyParamsToForm(initParams);
initSort();
loadFlights(initParams, 0);
