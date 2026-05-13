/* table-utils.js — shared table helpers (sort, pagination, photo, badges) */

/**
 * Set up click-to-sort on <th data-sort="..."> headers.
 * @param {string} tableId       — table element ID (e.g. "flights-table")
 * @param {object} sortState     — { col, dir } object (mutated in place)
 * @param {function} onSort      — called after sort state changes
 */
function initSortHeaders(tableId, sortState, onSort) {
  const selector = `#${tableId} thead th[data-sort]`;
  function updateIndicators() {
    document.querySelectorAll(selector).forEach(th => {
      const col = th.dataset.sort;
      th.classList.toggle("sort-asc",  col === sortState.col && sortState.dir === "asc");
      th.classList.toggle("sort-desc", col === sortState.col && sortState.dir === "desc");
    });
  }
  document.querySelectorAll(selector).forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (sortState.col === col) {
        sortState.dir = sortState.dir === "asc" ? "desc" : "asc";
      } else {
        sortState.col = col;
        sortState.dir = "desc";
      }
      updateIndicators();
      onSort();
    });
  });
  updateIndicators();
}

/**
 * Render pagination buttons.
 * @param {string} containerId  — element ID for the pagination container
 * @param {number} total        — total result count
 * @param {number} pageSize     — items per page
 * @param {number} offset       — current offset
 * @param {function} onPage     — called with target offset when a page button is clicked
 */
function renderPagination(containerId, total, pageSize, offset, onPage) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  if (total <= pageSize) return;

  const totalPages  = Math.ceil(total / pageSize);
  const currentPage = Math.floor(offset / pageSize);

  function makeBtn(label, targetOffset, disabled, active) {
    const btn = document.createElement("button");
    btn.textContent = label;
    if (disabled) btn.disabled = true;
    if (active) btn.classList.add("active");
    btn.addEventListener("click", () => onPage(targetOffset));
    return btn;
  }

  container.appendChild(makeBtn("\u2190 Prev", offset - pageSize, offset === 0, false));
  const start = Math.max(0, currentPage - 2);
  const end   = Math.min(totalPages - 1, currentPage + 2);
  if (start > 0) container.appendChild(makeBtn("1", 0, false, false));
  if (start > 1) { const span = document.createElement("span"); span.textContent = "\u2026"; container.appendChild(span); }
  for (let page = start; page <= end; page++) {
    container.appendChild(makeBtn(page + 1, page * pageSize, false, page === currentPage));
  }
  if (end < totalPages - 2) { const span = document.createElement("span"); span.textContent = "\u2026"; container.appendChild(span); }
  if (end < totalPages - 1) container.appendChild(makeBtn(totalPages, (totalPages - 1) * pageSize, false, false));
  container.appendChild(makeBtn("Next \u2192", offset + pageSize, offset + pageSize >= total, false));
}

/**
 * Allowlist URL schemes for href/src — returns the original URL if it begins
 * with https://, else "". Photo URLs come from third-party APIs (Planespotters,
 * airport-data, hexdb), all of which serve photos over HTTPS; rejecting plain
 * http:// closes the MITM window for users on hostile networks.
 */
function safeHttpUrl(url) {
  if (typeof url !== "string") return "";
  const trimmed = url.trim();
  return /^https:\/\//i.test(trimmed) ? trimmed : "";
}

/**
 * Build the " via <source>" suffix for the photo-credit line.  The source is
 * derived from the link URL's hostname so it stays accurate as the photo
 * fallback chain grows (Planespotters → airport-data.com → hexdb.io →
 * Wikipedia).  Returns "" when no link URL or unrecognised host.
 */
const _PHOTO_SOURCE_HOSTS = {
  "www.planespotters.net":  "Planespotters.net",
  "planespotters.net":      "Planespotters.net",
  "www.airport-data.com":   "airport-data.com",
  "airport-data.com":       "airport-data.com",
  "hexdb.io":               "hexdb.io",
  "en.wikipedia.org":       "Wikipedia",
};

function photoSourceSuffix(linkUrl) {
  if (!linkUrl) return "";
  try {
    const host = new URL(linkUrl).hostname.toLowerCase();
    const label = _PHOTO_SOURCE_HOSTS[host];
    return label ? ` via ${label}` : "";
  } catch (_e) { return ""; }
}

/**
 * Fetch and render an aircraft photo into a section element.
 * @param {number} flightId   — flight ID to fetch photo for
 * @param {string} sectionId  — element ID for the photo container (default "photo-section")
 */
async function loadPhoto(flightId, sectionId) {
  if (!flightId) return;
  try {
    const resp = await fetch(ROOT + "/api/flights/" + flightId + "/photo");
    if (!resp.ok || resp.status === 204) return;
    const photo = await resp.json();
    const thumb = safeHttpUrl(photo && photo.thumbnail_url);
    if (!thumb) return;
    const link = safeHttpUrl(photo.link_url) || safeHttpUrl(photo.large_url);
    const section = document.getElementById(sectionId || "photo-section");
    section.classList.remove("hidden");
    const img = `<img src="${escHtml(thumb)}" alt="Aircraft photo" class="aircraft-photo">`;
    const typeNote = photo.is_type_photo
      ? `<div class="photo-type-note">Photo: <em>${escHtml(photo.type_desc || photo.type_code || "")}</em> — not this specific aircraft</div>`
      : "";
    section.innerHTML = `
      ${link ? `<a href="${escHtml(link)}" target="_blank" rel="noopener">${img}</a>` : img}
      ${photo.photographer ? `<div class="photo-credit">&copy; ${escHtml(photo.photographer)}${photoSourceSuffix(link)}</div>` : ""}
      ${typeNote}
    `;
  } catch (err) { console.error("loadPhoto:", err); }
}

/**
 * Return a badge HTML string for military/interesting flags.
 * @param {number} flags      — bitmask (1 = military, 2 = interesting)
 * @param {string} style      — "short" (MIL/star), "long" (Military aircraft)
 */
const FLAG_MILITARY    = 1;
const FLAG_INTERESTING = 2;
const FLAG_ANONYMOUS   = 16;

function flagBadge(flags, style) {
  // Precedence: military > interesting > anonymous.  A non-ICAO military hex
  // shows the Military badge; the anon bit still rides along in the bitmask
  // so callers that want both can render them side-by-side.
  if (flags & FLAG_MILITARY) {
    const label = style === "short" ? "MIL" : "Military" + (style === "long" ? " aircraft" : "");
    return ` <span class="badge badge-mil" title="Military aircraft \u2014 armed forces of any country">${label}</span>`;
  }
  if (flags & FLAG_INTERESTING) {
    const label = style === "short" ? "\u2605" : "Interesting" + (style === "long" ? " aircraft" : "");
    return ` <span class="badge badge-int" title="Interesting aircraft \u2014 government, VIP, special mission, air ambulance">${label}</span>`;
  }
  if (flags & FLAG_ANONYMOUS) {
    const label = style === "short" ? "?" : "Anonymous" + (style === "long" ? " hex" : "");
    return ` <span class="badge badge-anon" title="Non-ICAO Mode-S address \u2014 hex falls outside every state-allocated block (likely military OPSEC, TIS-B, or test)">${label}</span>`;
  }
  return "";
}
