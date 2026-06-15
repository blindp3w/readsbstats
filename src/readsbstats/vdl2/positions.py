"""Parse precise aircraft positions out of ACARS message bodies.

Two formats, both query-time / read-only (same pattern as m1bpos.py / oooi.py):

1. **Label-16 AUTPOS** (:func:`parse_position`). vdlm2dec populates the ``lat``/``lon``
   columns only from coarse VDL2 XID link-layer frames (~0.1° ≈ 11 km); the precise
   positions (~0.001°) live as text inside Label-16 AUTPOS bodies, e.g.::

       WA921  ,N 52.166,E 020.772,4406, 251,2054, 72\\TS154458,050626
       153103,68416,1652, 150,N 52.180 E 20.086

   Extracts the first ``N/S dd.ddd  E/W ddd.ddd`` fix. A decimal fraction is required
   so stray integers in engine/telemetry noise don't match, and the result is
   range-checked. Returns ``None`` for non-conforming bodies (incl. the literal
   'no fix' marker ``N   .    MMMM.MMM``).

2. **LOT ``59,G`` ground telemetry** (:func:`parse_59g`). The position sub-form
   (ACARS label 36) carries a bare-decimal fix in fixed comma-separated fields::

       59,G,<HHMM>,1,1,<ICAO>,52.15,20.59,52.15,20.61,...

   A required decimal fraction on fields 6/7 (+ range-check) is the discriminator
   from the label-37 runway/status sub-form (whose fields 6/7 are blank or integer
   status codes, never ``dd.dd`` coordinates).
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


def parse_59g(body: object) -> dict | None:
    """Extract ``{"lat","lon"}`` from a LOT ``59,G,`` ground-telemetry body, or
    ``None``. The position sub-form (label 36) has a decimal-degree fix in fields 6/7
    (e.g. ``52.15``,``20.59``). A decimal fraction is **required** on both — same rule
    as :func:`parse_position` — so the label-37 runway/status sub-form (whose fields 6/7
    are blank or small integer codes, never ``dd.dd`` coords) is rejected structurally,
    not just incidentally."""
    if not isinstance(body, str) or not body.startswith("59,G,"):
        return None
    f = body.split(",")
    if len(f) < 8:
        return None
    s6, s7 = f[6].strip(), f[7].strip()
    if "." not in s6 or "." not in s7:   # integer/blank status codes are not coords
        return None
    try:
        lat, lon = float(s6), float(s7)
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return {"lat": lat, "lon": lon}
