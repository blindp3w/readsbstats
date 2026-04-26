/* watchlist.js — watchlist management page */

const TYPE_LABELS = { icao: "ICAO", registration: "Reg", callsign_prefix: "Prefix" };

async function loadWatchlist() {
  const tbody = document.getElementById("watchlist-body");
  try {
    const resp = await fetch(ROOT + "/api/watchlist");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    tbody.innerHTML = "";
    if (!data.entries.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="loading">No entries yet.</td></tr>';
      return;
    }
    for (const entry of data.entries) {
      const tr = document.createElement("tr");
      const added = entry.created_at
        ? new Date(entry.created_at * 1000).toLocaleDateString() : "—";
      const airborne = entry.airborne
        ? '<span class="badge badge-adsb" title="Currently in range">In range</span>' : "";
      tr.innerHTML = `
        <td><span class="badge badge-other">${TYPE_LABELS[entry.match_type] || entry.match_type}</span></td>
        <td><strong>${escHtml(entry.value.toUpperCase())}</strong></td>
        <td>${entry.label ? escHtml(entry.label) : "—"}</td>
        <td>${added}</td>
        <td>${airborne}</td>
        <td><button class="btn-delete" data-id="${entry.id}" title="Remove">✕</button></td>
      `;
      tbody.appendChild(tr);
    }
    tbody.querySelectorAll(".btn-delete").forEach(btn => {
      btn.addEventListener("click", () => deleteEntry(parseInt(btn.dataset.id)));
    });
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" class="loading" style="color:var(--red)">Error: ${escHtml(err.message)}</td></tr>`;
  }
}

async function deleteEntry(id) {
  try {
    const resp = await fetch(ROOT + "/api/watchlist/" + id, {
      method:  "DELETE",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    loadWatchlist();
  } catch (err) {
    alert("Delete failed: " + err.message);
  }
}

document.getElementById("add-form").addEventListener("submit", async evt => {
  evt.preventDefault();
  const errEl = document.getElementById("add-error");
  errEl.textContent = "";

  const matchType = document.getElementById("add-type").value;
  const value     = document.getElementById("add-value").value.trim();
  const label     = document.getElementById("add-label").value.trim() || null;

  if (!value) { errEl.textContent = "Value is required."; return; }
  if (matchType === "icao" && !/^[0-9a-fA-F]{6}$/.test(value)) {
    errEl.textContent = "ICAO hex must be exactly 6 hexadecimal characters.";
    return;
  }

  try {
    const resp = await fetch(ROOT + "/api/watchlist", {
      method:  "POST",
      headers: {
        "Content-Type":     "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body:    JSON.stringify({ match_type: matchType, value, label }),
    });
    if (resp.status === 409) { errEl.textContent = "Already in watchlist."; return; }
    if (!resp.ok) { errEl.textContent = "Error " + resp.status; return; }
    document.getElementById("add-value").value = "";
    document.getElementById("add-label").value = "";
    loadWatchlist();
  } catch (err) {
    errEl.textContent = "Request failed: " + err.message;
  }
});

loadWatchlist();
