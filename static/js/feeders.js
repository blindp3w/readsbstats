function applyFeederUnits() {
  document.querySelectorAll(".feeder-dist[data-dist-nm]").forEach(function(el) {
    const nm = parseFloat(el.dataset.distNm);
    if (!isNaN(nm)) el.textContent = fmtDist(nm);
  });
}

document.addEventListener("DOMContentLoaded", applyFeederUnits);
window.addEventListener("unitschange", applyFeederUnits);
