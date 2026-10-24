"""ADS-C (FANS-1/A over ACARS, label B6) position reports from dumpvdl2 ``raw`` JSON.

ADS-C reports ride ACARS label B6 as ARINC 622 binary, so the ``body`` is an opaque
hex blob — but dumpvdl2 (+ libacars) decodes it, and the decoded tags live only in
``raw``. The first ``basic_report`` tag carries a precise (decimal-degree) fix; validated
against ADS-B tracks of the same airframe at ~0.11 nm median error (114 rows / 90 d)::

    vdl2.avlc.acars.arinc622.adsc.tags[] -> {"basic_report": {"lat", "lon", "alt", ...}}

Like ``positions.py`` / ``m1bpos.py`` this is read-only and parsed at **query time**, but
from ``raw`` rather than ``body``. ``raw`` may be truncated (``RSBS_VDL2_RAW_MAX``), in
which case the JSON no longer parses and the row simply yields no point.
"""
from __future__ import annotations

import json
import math

from ..cleaners import valid_lat, valid_lon


def parse_basic_report(raw: object) -> dict | None:
    """``{"lat", "lon", "alt"}`` (lat/lon 5-dp rounded, ``alt`` int feet or ``None``) from
    the stored dumpvdl2 ``raw`` JSON string, or ``None`` when it is absent, truncated,
    structurally different, carries no ``basic_report`` tag, or the fix is out of range.
    Every level is guarded, so any structural miss yields ``None``."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        node = json.loads(raw)
    except ValueError:
        return None
    for key in ("vdl2", "avlc", "acars", "arinc622", "adsc"):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    tags = node.get("tags") if isinstance(node, dict) else None
    if not isinstance(tags, list):
        return None
    for tag in tags:
        br = tag.get("basic_report") if isinstance(tag, dict) else None
        if not isinstance(br, dict):
            continue
        lat, lon = valid_lat(br.get("lat")), valid_lon(br.get("lon"))
        if lat is None or lon is None:
            return None
        alt = br.get("alt")
        ok = isinstance(alt, (int, float)) and not isinstance(alt, bool) and math.isfinite(alt)
        return {"lat": round(lat, 5), "lon": round(lon, 5), "alt": int(alt) if ok else None}
    return None
