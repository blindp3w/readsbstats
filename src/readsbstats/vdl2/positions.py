"""Parse precise aircraft positions out of ACARS message bodies (Label-16 AUTPOS).

Validated against a real vdlm2dec feed: vdlm2dec populates the ``lat``/``lon``
columns only from coarse VDL2 XID link-layer frames (~0.1° ≈ 11 km). The precise
positions (~0.001°) live as text inside Label-16 AUTPOS bodies, e.g.::

    WA921  ,N 52.166,E 020.772,4406, 251,2054, 72\\TS154458,050626
    153103,68416,1652, 150,N 52.180 E 20.086

This extracts the first ``N/S dd.ddd  E/W ddd.ddd`` fix from a body. A decimal
fraction is required so stray integers in engine/telemetry noise don't match,
and the result is range-checked. Returns ``None`` for non-conforming bodies
(including the literal 'no fix' marker ``N   .    MMMM.MMM``).
"""
from __future__ import annotations

import re

# N/S then decimal degrees, a comma or space separator, then E/W then decimal
# degrees. Decimal fraction required on both to avoid matching bare integers.
_POS = re.compile(
    r"([NS])\s*(\d{1,2}\.\d+)\s*[, ]\s*([EW])\s*(\d{1,3}\.\d+)"
)


def parse_position(body: object) -> dict | None:
    """Extract ``{"lat": float, "lon": float}`` from an ACARS body, or ``None``
    if it carries no recognisable decimal-degree fix."""
    if not isinstance(body, str) or not body:
        return None
    m = _POS.search(body)
    if m is None:
        return None
    ns, lat_s, ew, lon_s = m.groups()
    lat = float(lat_s) * (1 if ns == "N" else -1)
    lon = float(lon_s) * (1 if ew == "E" else -1)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return {"lat": lat, "lon": lon}
