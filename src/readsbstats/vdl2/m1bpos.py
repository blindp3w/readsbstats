"""Parse #M1BPOS (Honeywell FMS) ACARS bodies: precise ddmmm position + filed route.

#M1BPOS carries the richest decodable content on the feed but in two distinct
shapes the client-side airframes decoder does not both cover:

1. **Position** — ``#M1BPOS<NS>dd mmm <EW>ddd mmm`` where the minute field is
   minutes×10 (ddmmm, capped 599 = 59.9'), e.g. ``N52081E020017`` =
   N 52deg08.1' E 020deg01.7'. NOT decimal degrees. Used to add precise map fixes
   alongside Label-16 AUTPOS.

2. **Filed route** — an optional ``/RP:DA:..:AA:..:CR:..:D:..:A:..:AP:..`` block
   (~21/83 bodies). The decoder returns decoded=false on exactly these, so this
   is the only path to the filed route. Surfaced on the flight ACARS panel.

Read-only, parsed at query time (same pattern as oooi.py / positions.py).
"""
from __future__ import annotations

import re

from ..cleaners import clean_short_text, valid_lat, valid_lon

# #M1BPOS then N/S dd mmm  E/W ddd mmm. Anchored: the body must start with the tag.
_POS = re.compile(r"#M1BPOS([NS])(\d{2})(\d{3})([EW])(\d{3})(\d{3})")


def parse_position(body: object) -> dict | None:
    """Extract ``{"lat": float, "lon": float}`` (5-dp rounded) from a #M1BPOS
    body, or ``None`` if it carries no valid ddmmm fix."""
    if not isinstance(body, str) or not body:
        return None
    m = _POS.match(body)
    if m is None:
        return None
    ns, dd, lat_min, ew, ddd, lon_min = m.groups()
    if int(lat_min) >= 600 or int(lon_min) >= 600:
        return None  # 600-999 are invalid ddmmm minute values (max valid = 599 = 59.9')
    lat = (int(dd) + (int(lat_min) / 10) / 60) * (1 if ns == "N" else -1)
    lon = (int(ddd) + (int(lon_min) / 10) / 60) * (1 if ew == "E" else -1)
    lat = valid_lat(round(lat, 5))
    lon = valid_lon(round(lon, 5))
    if lat is None or lon is None:
        return None
    return {"lat": lat, "lon": lon}


# /RP: TEI → output field. Dict lookup is exact-match, so single-letter 'A'
# (STAR) never collides with two-letter 'AA'/'DA'/'AP'.
_RP_KEYS = {
    "DA": "dep", "AA": "arr", "CR": "company_route",
    "D": "sid", "A": "star", "AP": "approach",
}
_ROUTE_CAP = 200  # company_route strings run ~70 chars; airports/procedures far shorter


def parse_route(body: object) -> dict | None:
    """Extract the filed route from a #M1BPOS ``/RP:`` block. Returns a dict with
    ``dep``/``arr`` (required) plus any of ``company_route``/``sid``/``star``/
    ``approach`` present, or ``None`` when there is no parseable route."""
    if not isinstance(body, str) or not body.startswith("#M1BPOS"):
        return None
    i = body.find("/RP:")
    if i < 0:
        return None
    tokens = body[i + 4:].split(":")  # values never contain ':'
    out: dict[str, str] = {}
    j = 0
    while j < len(tokens):
        key = tokens[j].strip()
        if key in _RP_KEYS and j + 1 < len(tokens):
            field = _RP_KEYS[key]
            value = clean_short_text(tokens[j + 1].strip(), _ROUTE_CAP)
            if value and field not in out:   # first occurrence wins
                out[field] = value
            j += 2
        else:
            j += 1  # unknown key or stray token; route-field values never equal a key
    if "dep" not in out or "arr" not in out:
        return None
    return out
