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

from ..cleaners import valid_lat, valid_lon

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
