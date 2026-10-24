"""Parse precise aircraft positions out of ACARS message bodies.

Several formats (label-16 AUTPOS, LOT ``59,G`` telemetry and the per-label airline
parsers below), all query-time / read-only (same pattern as m1bpos.py / oooi.py):

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

3. **More airline/vendor formats**, each validated against ADS-B tracks of the same
   airframe (nearest fix within ±60 s of receive time; reception ``ts`` is the point
   time — body times don't improve accuracy):

   - label 17 (Wizz Air) ``…,15720,N 52.464,E 20.842,22OCT26,…`` — :func:`parse_label17`
     (~0.9 nm). Rejected when comma-field 3 (altitude) is not an integer: the ``------``
     rows carry the DEPARTURE airport's coordinates, up to ~1200 nm off.
   - label 38 (Finnair) ``/AY1476/240926/E 20.735/N 52.402/…`` — :func:`parse_label38`
     (~1 nm); lon comes FIRST.
   - label 2F ``1310+5219+2087/0130`` — :func:`parse_label2f` (~0.5 nm); lat/lon are
     hundredths of a decimal degree (NOT degrees+minutes, which measured 19.5 nm off).
   - label 14 ``…,LAT N 52.014,LON E 20.079,…`` — :func:`parse_label14` (~0.7 nm).
   - label B0 FANS AFN logon ``…/FPON52106E020556,1/…`` — :func:`parse_afn_fpo` (~0.1 nm);
     ddmmm like m1bpos (minutes×10).
   - labels 10,13,18,27,33,12,43,41,2P carry the label-16 style fix (~0.7 nm).

   :data:`PRECISE_LABEL_PARSERS` maps each label to its parser and :func:`parse_by_label`
   dispatches, so a coordinate-looking body under any other label is never trusted.
   (ADS-C label B6 lives in ``raw`` JSON, not the body — see ``adsc.py``.)
"""
from __future__ import annotations

import re
from typing import Callable

from ..cleaners import valid_lat, valid_lon

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


_INT = re.compile(r"-?\d+")


def _fix(ns: str, lat_v: float, ew: str, lon_v: float) -> dict | None:
    """Signed, range-checked, 5-dp ``{"lat","lon"}`` from hemisphere letters + magnitudes."""
    lat = valid_lat(round(lat_v * (1 if ns in ("N", "+") else -1), 5))
    lon = valid_lon(round(lon_v * (1 if ew in ("E", "+") else -1), 5))
    if lat is None or lon is None:
        return None
    return {"lat": lat, "lon": lon}


def parse_label17(body: object) -> dict | None:
    """Label 17 (Wizz Air) ``HHMMSS,…,<alt>,N 52.464,E 20.842,22OCT26,…`` -> ``{"lat","lon"}``.
    The generic ``N dd.ddd E ddd.ddd`` fix, but only when comma-field 3 (altitude) is an
    integer: ``------`` rows carry the departure airport's coordinates, not the aircraft's."""
    if not isinstance(body, str) or not body:
        return None
    f = body.split(",")
    if len(f) < 4 or _INT.fullmatch(f[3].strip()) is None:
        return None
    return parse_position(body)


_L38 = re.compile(r"/([EW])\s*(\d{1,3}\.\d+)/([NS])\s*(\d{1,2}\.\d+)/")


def parse_label38(body: object) -> dict | None:
    """Label 38 (Finnair) ``/AY1476/240926/E 20.735/N 52.402/36945/…`` -> ``{"lat","lon"}``
    (note: longitude comes first)."""
    if not isinstance(body, str) or not body:
        return None
    m = _L38.search(body)
    if m is None:
        return None
    ew, lon_s, ns, lat_s = m.groups()
    return _fix(ns, float(lat_s), ew, float(lon_s))


_L2F = re.compile(r"(\d{4})([+-])(\d{4})([+-])(\d{4,5})/")


def parse_label2f(body: object) -> dict | None:
    """Label 2F ``1310+5219+2087/0130`` -> ``{"lat","lon"}``: HHMM, then signed lat and lon in
    **hundredths of a decimal degree** (5219 = 52.19, 2087 = 20.87; 12345 = 123.45) — not
    degrees+minutes (that reading measured 19.5 nm off)."""
    if not isinstance(body, str) or not body:
        return None
    m = _L2F.match(body)
    if m is None:
        return None
    _hhmm, lat_sign, lat_s, lon_sign, lon_s = m.groups()
    return _fix(lat_sign, int(lat_s) / 100, lon_sign, int(lon_s) / 100)


_L14 = re.compile(r"LAT ([NS])\s*(\d+\.\d+),\s*LON ([EW])\s*(\d+\.\d+)")


def parse_label14(body: object) -> dict | None:
    """Label 14 ``PRESENT POSITION REPORT … LAT N 52.014,LON E 20.079,…`` -> ``{"lat","lon"}``."""
    if not isinstance(body, str) or not body:
        return None
    m = _L14.search(body)
    if m is None:
        return None
    ns, lat_s, ew, lon_s = m.groups()
    return _fix(ns, float(lat_s), ew, float(lon_s))


_FPO = re.compile(r"/FPO([NS])(\d{2})(\d{3})([EW])(\d{3})(\d{3})")


def parse_afn_fpo(body: object) -> dict | None:
    """Label B0 FANS AFN logon ``…/FPON52106E020556,1/…`` -> ``{"lat","lon"}``: ddmmm with the
    minute field = minutes×10 (``106`` = 10.6'), each < 600 — same rule as m1bpos."""
    if not isinstance(body, str) or not body:
        return None
    m = _FPO.search(body)
    if m is None:
        return None
    ns, dd, lat_min, ew, ddd, lon_min = m.groups()
    if int(lat_min) >= 600 or int(lon_min) >= 600:
        return None
    return _fix(ns, int(dd) + (int(lat_min) / 10) / 60, ew, int(ddd) + (int(lon_min) / 10) / 60)


# ACARS label -> body parser for every label with a validated precise body fix. The
# label-16-style generic group (10,13,18,27,33,12,43,41,2P) shares parse_position.
PRECISE_LABEL_PARSERS: dict[str, Callable[[object], dict | None]] = {
    "16": parse_position,
    **dict.fromkeys(("10", "13", "18", "27", "33", "12", "43", "41", "2P"), parse_position),
    "17": parse_label17,
    "38": parse_label38,
    "2F": parse_label2f,
    "14": parse_label14,
    "B0": parse_afn_fpo,
}


def parse_by_label(label: object, body: object) -> dict | None:
    """Parse ``body`` with the parser registered for ``label``; ``None`` for any other label."""
    parser = PRECISE_LABEL_PARSERS.get(label) if isinstance(label, str) else None
    return parser(body) if parser is not None else None
