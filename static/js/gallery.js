/* gallery.js — flagged aircraft card gallery */

const PAGE_SIZE = 24;
let currentOffset = 0;
let currentTotal  = 0;
let currentFilter = "all";
let currentSort   = "last_seen";
let currentDir    = "desc";

function buildUrl(offset) {
  const params = new URLSearchParams({
    limit:    PAGE_SIZE,
    offset,
    sort_by:  currentSort,
    sort_dir: currentDir,
  });
  if (currentFilter !== "all") params.set("flags", currentFilter);
  return ROOT + "/api/aircraft/flagged?" + params;
}

function renderCard(ac) {
  const card = document.createElement("a");
  card.href = ROOT + "/aircraft/" + encodeURIComponent(ac.icao_hex);
  card.className = "gallery-card";

  const badge = flagBadge(ac.flags, "short");
  const reg = ac.registration ? escHtml(ac.registration) : escHtml(ac.icao_hex);
  const typeStr = [ac.aircraft_type, ac.type_desc].filter(Boolean).map(escHtml).join(" — ");
  const country = escHtml(ac.country || "Unknown");
  const flights = ac.flight_count.toLocaleString();
  const lastSeen = ac.last_seen
    ? new Date(ac.last_seen * 1000).toLocaleDateString() : "—";

  const photoId = "photo-" + ac.icao_hex;

  card.innerHTML = `
    <div class="gallery-photo" id="${photoId}">
      ${ac.thumbnail_url
        ? `<img src="${escHtml(ac.thumbnail_url)}" alt="${reg}" loading="lazy">`
        : '<div class="gallery-no-photo">No photo</div>'}
    </div>
    <div class="gallery-info">
      <div class="gallery-reg">${reg}${badge}</div>
      <div class="gallery-type">${typeStr || "Unknown type"}</div>
      <div class="gallery-country">${country}</div>
      <div class="gallery-meta">
        <span>${flights} flight${ac.flight_count !== 1 ? "s" : ""}</span>
        <span>Last: ${lastSeen}</span>
      </div>
    </div>
  `;

  // Lazy-load photo if not already cached in API response
  if (!ac.thumbnail_url) {
    loadAircraftPhoto(ac.icao_hex, photoId);
  }

  return card;
}

async function loadAircraftPhoto(icaoHex, containerId) {
  try {
    const resp = await fetch(ROOT + "/api/aircraft/" + encodeURIComponent(icaoHex) + "/photo");
    if (!resp.ok) return;
    const photo = await resp.json();
    if (!photo || !photo.thumbnail_url) return;
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = `<img src="${escHtml(photo.thumbnail_url)}" alt="Aircraft" loading="lazy">`;
  } catch (err) { console.error("loadAircraftPhoto:", err); }
}

function renderGrid(aircraft) {
  const grid = document.getElementById("gallery-grid");
  grid.innerHTML = "";
  if (aircraft.length === 0) {
    grid.innerHTML = '<div class="loading">No flagged aircraft found.</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  aircraft.forEach(ac => frag.appendChild(renderCard(ac)));
  grid.appendChild(frag);
}

async function load(offset) {
  currentOffset = offset;
  const grid = document.getElementById("gallery-grid");
  grid.innerHTML = '<div class="loading">Loading…</div>';
  document.getElementById("pagination").innerHTML = "";
  document.getElementById("result-info").textContent = "";

  try {
    const resp = await fetch(buildUrl(offset));
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();

    currentTotal = data.total;
    renderGrid(data.aircraft);
    document.getElementById("result-info").textContent =
      data.total.toLocaleString() + " aircraft";
    renderPagination("pagination", data.total, PAGE_SIZE, offset, o => load(o));
  } catch (err) {
    grid.innerHTML = `<div class="loading" style="color:var(--red)">Error: ${escHtml(err.message)} <button class="btn-retry">Retry</button></div>`;
    grid.querySelector(".btn-retry").addEventListener("click", () => load(currentOffset));
  }
}

// Filter buttons
document.querySelectorAll(".gallery-filters .range-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".gallery-filters .range-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    currentFilter = btn.dataset.filter;
    load(0);
  });
});

// Sort select
document.getElementById("gallery-sort-select").addEventListener("change", function() {
  const [col, dir] = this.value.split(":");
  currentSort = col;
  currentDir = dir;
  load(0);
});

load(0);
