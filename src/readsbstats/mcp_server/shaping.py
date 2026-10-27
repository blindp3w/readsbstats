"""Per-tool response shaping through output allowlists (ADR-0022, Decision 6).

Raw API payloads carry the receiver's coordinates, flight bounding boxes,
track points and many fields a model does not need. No absolute coordinates
leave the server: aircraft are reported by distance and bearing only. Every tool builds its
result from the allowlists below, then passes it through :func:`strip_location`
(a backstop) and :func:`cap_size`. Free text from third parties is cleaned and
length-capped; times become local ISO strings; flag bits become names.
"""

import json
import math
import re
from typing import Any

from .. import cleaners, config, geo
from .timeparse import to_local_iso

TEXT_LIMIT = 80
CODE_LIMIT = 12
MAX_RESPONSE_BYTES = 48_000
TOP_N = 10
_SPARK_POINTS = 48

FLAG_NAMES = {
    config.FLAG_MILITARY: "military",
    config.FLAG_INTERESTING: "interesting",
    config.FLAG_PIA: "pia",
    config.FLAG_LADD: "ladd",
    config.FLAG_ANONYMOUS: "anonymous",
}

_LOCATION_KEYS = frozenset({"receiver_lat", "receiver_lon", "lat_min", "lat_max",
                            "lon_min", "lon_max", "lat", "lon", "latitude", "longitude"})


# ---- scalar helpers ---------------------------------------------------------
def decode_flags(value: Any) -> list[str]:
    if type(value) is not int:
        return []
    return [name for bit, name in FLAG_NAMES.items() if value & bit]


def _text(value: Any, limit: int = TEXT_LIMIT) -> str | None:
    return cleaners.clean_short_text(value, limit)


def _num(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _rnd(value: Any, ndigits: int | None = None) -> int | float | None:
    v = _num(value)
    if v is None:
        return None
    return round(v) if ndigits is None else round(v, ndigits)


def _iso(value: Any) -> str | None:
    v = _num(value)
    if v is None:
        return None
    try:
        return to_local_iso(v)
    except (OverflowError, OSError, ValueError):
        return None


def _put(out: dict, key: str, value: Any) -> None:
    if value is not None:
        out[key] = value


def _build(row: dict, spec: tuple) -> dict:
    """spec: (out_key, src_key, kind) with kind in t/c/n/i/iso/flags/hex/bool/url."""
    out: dict = {}
    for out_key, src, kind in spec:
        v = row.get(src)
        if kind == "t":
            v = _text(v)
        elif kind == "c":
            v = _text(v, CODE_LIMIT)
        elif kind == "n":
            v = _num(v)
        elif kind == "i":
            v = _rnd(v)
        elif kind == "iso":
            v = _iso(v)
        elif kind == "hex":
            v = cleaners.valid_icao_hex(v)
        elif kind == "bool":
            v = None if v is None else bool(v)
        elif kind == "flags":
            v = decode_flags(v) or None
        _put(out, out_key, v)
    return out


# ---- flights ----------------------------------------------------------------
def flight_row(r: Any) -> dict | None:
    if not isinstance(r, dict):
        return None
    out = _build(r, (
        ("id", "id", "i"), ("icao_hex", "icao_hex", "hex"), ("callsign", "callsign", "t"),
        ("registration", "registration", "t"), ("aircraft_type", "aircraft_type", "c"),
        ("type_desc", "type_desc", "t"), ("airline_name", "airline_name", "t"),
        ("flags", "flags", "flags"), ("squawk", "squawk", "c"),
        ("primary_source", "primary_source", "c"),
        ("first_seen", "first_seen", "iso"), ("last_seen", "last_seen", "iso"),
    ))
    duration = _num(r.get("duration_sec"))
    if duration is None:
        a, b = _num(r.get("first_seen")), _num(r.get("last_seen"))
        duration = None if a is None or b is None else b - a
    _put(out, "duration_min", None if duration is None else round(duration / 60))
    _put(out, "max_alt_ft", _rnd(r.get("max_alt_baro")))
    _put(out, "max_gs_kt", _rnd(r.get("max_gs")))
    _put(out, "max_distance_nm", _rnd(r.get("max_distance_nm"), 1))
    out.update(_build(r, (
        ("origin_icao", "origin_icao", "c"), ("origin_name", "origin_name", "t"),
        ("dest_icao", "dest_icao", "c"), ("dest_name", "dest_name", "t"),
        ("has_acars", "has_acars", "bool"),
    )))
    return out


def aircraft_info(info: Any) -> dict:
    info = info if isinstance(info, dict) else {}
    out = _build(info, (
        ("registration", "registration", "t"), ("type", "type_code", "c"),
        ("type_desc", "type_desc", "t"), ("flags", "flags", "flags"),
        ("total_flights", "total_flights", "i"),
        ("first_seen", "first_seen", "iso"), ("last_seen", "last_seen", "iso"),
        ("country", "country", "t"),
    ))
    secs = _num(info.get("total_duration_sec"))
    _put(out, "total_hours", None if secs is None else round(secs / 3600, 1))
    return out


def shape_profile(raw: dict) -> dict:
    points = []
    for p in raw.get("positions") or []:
        if not isinstance(p, dict):
            continue
        points.append(_build(p, (
            ("time", "ts", "iso"), ("alt_ft", "alt_baro", "i"), ("gs_kt", "gs", "i"),
            ("track_deg", "track", "i"), ("vrate_fpm", "baro_rate", "i"),
            ("source", "source_type", "c"),
        )))
    return {"total_positions": _rnd(raw.get("total")), "points": points}


# ---- live / snapshot --------------------------------------------------------
def live_aircraft(a: dict, rlat: Any, rlon: Any) -> dict:
    """One aircraft. Distance and bearing are computed from the exact receiver
    location; neither that location nor the aircraft's own coordinates are
    returned (an absolute position plus a distance and bearing would give the
    receiver's position away)."""
    out = _build(a, (
        ("icao_hex", "icao_hex", "hex"), ("flight_id", "flight_id", "i"),
        ("callsign", "callsign", "t"), ("registration", "registration", "t"),
        ("aircraft_type", "aircraft_type", "c"), ("flags", "flags", "flags"),
        ("origin_icao", "origin_icao", "c"), ("dest_icao", "dest_icao", "c"),
        ("alt_ft", "alt_baro", "i"), ("gs_kt", "gs", "i"), ("track_deg", "track", "i"),
        ("seconds_ago", "seconds_ago", "i"),
    ))
    source = a.get("source_type") or a.get("primary_source")
    _put(out, "source", _text(source, CODE_LIMIT))
    lat, lon = _num(a.get("lat")), _num(a.get("lon"))
    if lat is not None and lon is not None:
        rl, ro = _num(rlat), _num(rlon)
        if rl is not None and ro is not None:
            out["distance_nm"] = round(geo.haversine_nm(rl, ro, lat, lon), 1)
            out["bearing_deg"] = round(geo.bearing(rl, ro, lat, lon)) % 360
    return out


def shape_sky(raw: dict, limit: int) -> dict:
    rlat, rlon = raw.get("receiver_lat"), raw.get("receiver_lon")
    rows = [live_aircraft(a, rlat, rlon) for a in raw.get("aircraft") or []
            if isinstance(a, dict)]
    rows.sort(key=lambda r: (r.get("distance_nm") is None, r.get("distance_nm") or 0.0))
    out: dict = {}
    if "at" in raw:
        _put(out, "at", _iso(raw.get("at")))
    if "is_live" in raw:
        out["is_live"] = bool(raw.get("is_live"))
    out["count"] = len(rows)
    out["aircraft"] = rows[:limit]
    if len(rows) > limit:
        out["truncated"] = True
    return out


# ---- stats ------------------------------------------------------------------
def _rows(spec: tuple, limit: int | None = TOP_N):
    def shape(value: Any) -> list:
        rows = value if isinstance(value, list) else []
        return [_build(r, spec) for r in rows[:limit] if isinstance(r, dict)]
    return shape


def _new_aircraft(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    items = [_build(r, (
        ("icao_hex", "icao_hex", "hex"), ("registration", "registration", "t"),
        ("aircraft_type", "aircraft_type", "c"), ("type_desc", "type_desc", "t"),
        ("flags", "flags", "flags"), ("first_seen", "first_seen_ever", "iso"),
    )) for r in (value.get("items") or [])[:TOP_N] if isinstance(r, dict)]
    return {"total": _rnd(value.get("total")), "items": items}


def _furthest(value: Any) -> dict | None:
    out = flight_row(value)
    if out is None:
        return None
    _put(out, "record_set_at", _iso(value.get("record_set_at")))
    return out


def _trends(raw: dict) -> dict | None:
    out: dict = {}
    for key in ("flights_last_24h", "flights_last_7d"):
        _put(out, key, _rnd(raw.get(key)))
    if isinstance(raw.get("trends"), dict):
        out.update(_build(raw["trends"], (("flights_24h_prev", "flights_24h_prev", "i"),
                                          ("flights_7d_prev", "flights_7d_prev", "i"))))
    pw = raw.get("previous_window")
    if isinstance(pw, dict):
        prev = _build(pw, (("from", "from_ts", "iso"), ("to", "to_ts", "iso"),
                           ("total_flights", "total_flights", "i"),
                           ("total_positions", "total_positions", "i"),
                           ("unique_aircraft", "unique_aircraft", "i")))
        out["previous_window"] = prev
    return out or None


def _counts(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {str(k)[:16]: n for k, v in value.items() if (n := _num(v)) is not None}


_SUMMARY_SPEC = (
    ("total_flights", "total_flights", "i"), ("total_positions", "total_positions", "i"),
    ("unique_aircraft", "unique_aircraft", "i"), ("unique_airlines", "unique_airlines", "i"),
    ("oldest_flight", "oldest_flight", "iso"),
    ("military_flights", "military_flights", "i"),
    ("interesting_flights", "interesting_flights", "i"),
    ("anonymous_flights", "anonymous_flights", "i"),
)

_SECTION_ROWS = {
    "top_airlines": _rows((("airline", "airline", "c"), ("airline_name", "airline_name", "t"),
                           ("flights", "flights", "i"), ("unique_aircraft", "unique_aircraft", "i"))),
    "top_aircraft_types": _rows((("type", "type", "c"), ("type_desc", "type_desc", "t"),
                                 ("flights", "flights", "i"),
                                 ("unique_aircraft", "unique_aircraft", "i"))),
    "top_routes": _rows((("origin_icao", "origin_icao", "c"), ("dest_icao", "dest_icao", "c"),
                         ("origin_name", "origin_name", "t"), ("dest_name", "dest_name", "t"),
                         ("flights", "flights", "i"))),
    "top_airports": _rows((("icao_code", "icao_code", "c"), ("name", "name", "t"),
                           ("country", "country", "t"), ("appearances", "appearances", "i"))),
    "top_countries": _rows((("country", "country", "t"), ("flights", "flights", "i"),
                            ("unique_aircraft", "unique_aircraft", "i"))),
    "frequent_aircraft": _rows((("icao_hex", "icao_hex", "hex"), ("registration", "registration", "t"),
                                ("aircraft_type", "aircraft_type", "c"), ("type_desc", "type_desc", "t"),
                                ("flags", "flags", "flags"), ("flights", "flights", "i"),
                                ("last_seen", "last_seen", "iso"))),
    "hourly_distribution": _rows((("hour", "hour", "i"), ("count", "count", "i")), None),
    "daily_unique_aircraft": _rows((("day", "day", "c"), ("unique_aircraft", "unique_aircraft", "i"),
                                    ("flights", "flights", "i")), None),
    "altitude_distribution": _rows((("band", "band", "t"), ("count", "count", "i")), None),
    "heatmap": _rows((("weekday", "dow", "i"), ("hour", "hour", "i"), ("count", "count", "i")), None),
}

# name -> function(raw) -> value; "summary" is flattened into the result.
STATS_SECTIONS = {
    "summary": None,
    **{name: (lambda raw, n=name: _SECTION_ROWS[n](raw.get(n))) for name in _SECTION_ROWS},
    "source_breakdown": lambda raw: _counts(raw.get("source_breakdown")),
    "squawk_counts": lambda raw: _counts(raw.get("squawk_counts")),
    "new_aircraft": lambda raw: _new_aircraft(raw.get("new_aircraft")),
    "furthest_aircraft": lambda raw: _furthest(raw.get("furthest_aircraft")),
    "trends": _trends,
}
DEFAULT_STATS_SECTIONS = ("summary", "top_airlines", "top_aircraft_types", "top_routes",
                          "top_airports")


def shape_stats(raw: dict, sections: list[str] | None) -> dict:
    names = list(dict.fromkeys(sections or DEFAULT_STATS_SECTIONS))
    unknown = [n for n in names if n not in STATS_SECTIONS]
    if unknown:
        raise ValueError(f"unknown section {unknown[0]!r}; valid sections: "
                         + ", ".join(STATS_SECTIONS))
    out: dict = {}
    for name in names:
        if name == "summary":
            out.update(_build(raw, _SUMMARY_SPEC))
            continue
        value = STATS_SECTIONS[name](raw)
        if value is not None:
            out[name] = value
    return out


# ---- receiver metrics / health ---------------------------------------------
def _downsample(ts: list, vals: list) -> list:
    pairs = [(t, v) for t, v in zip(ts, vals) if _num(v) is not None]
    if len(pairs) <= _SPARK_POINTS:
        return [[_iso(t), _rnd(v, 2)] for t, v in pairs]
    n = len(pairs)
    points = []
    for i in range(_SPARK_POINTS):
        chunk = pairs[i * n // _SPARK_POINTS:(i + 1) * n // _SPARK_POINTS]
        if chunk:
            points.append([_iso(chunk[0][0]), round(sum(v for _, v in chunk) / len(chunk), 2)])
    return points


def shape_metrics(raw: dict) -> dict:
    data = raw.get("data") or []
    names = raw.get("metrics") or []
    ts = data[0] if data else []
    metrics = {}
    for i, name in enumerate(names, start=1):
        col = data[i] if i < len(data) else []
        vals = [v for v in col if _num(v) is not None]
        metrics[str(name)] = {
            "min": _rnd(min(vals), 2) if vals else None,
            "avg": _rnd(sum(vals) / len(vals), 2) if vals else None,
            "max": _rnd(max(vals), 2) if vals else None,
            "last": _rnd(vals[-1], 2) if vals else None,
            "points": _downsample(ts, col),
        }
    return {"bucket_seconds": _rnd(raw.get("bucket_seconds")), "metrics": metrics}


def shape_health(raw: dict) -> dict:
    checks = [_build(c, (("name", "name", "t"), ("severity", "severity", "c"),
                         ("message", "message", "t"), ("value", "value", "n"),
                         ("threshold", "threshold", "n")))
              for c in raw.get("checks") or [] if isinstance(c, dict)]
    out = {"overall": _text(raw.get("overall"), CODE_LIMIT)}
    _put(out, "as_of", _iso(raw.get("as_of")))
    out["checks"] = checks
    return out


def shape_feeders(raw: dict) -> list[dict]:
    return [_build(f, (("name", "name", "t"), ("systemd", "systemd", "c"),
                       ("port_status", "port_status", "c"), ("overall", "overall", "c")))
            for f in raw.get("feeders") or [] if isinstance(f, dict)]


# ---- misc -------------------------------------------------------------------
def shape_records(raw: dict) -> dict:
    return {k: flight_row(raw.get(k)) for k in ("furthest", "fastest", "highest", "longest")}


def shape_daily(raw: dict, days: int) -> dict:
    rows = [r for r in raw.get("dates") or [] if isinstance(r, dict)]
    return {"days": [_build(r, (("date", "date", "c"), ("flights", "flight_count", "i")))
                     for r in rows[:days]]}


def shape_flagged(raw: dict) -> dict:
    rows = [_build(a, (
        ("icao_hex", "icao_hex", "hex"), ("registration", "registration", "t"),
        ("aircraft_type", "aircraft_type", "c"), ("type_desc", "type_desc", "t"),
        ("flags", "flags", "flags"), ("country", "country", "t"),
        ("flight_count", "flight_count", "i"),
        ("first_seen", "first_seen", "iso"), ("last_seen", "last_seen", "iso"),
    )) for a in raw.get("aircraft") or [] if isinstance(a, dict)]
    return {"total": _rnd(raw.get("total")), "count": len(rows), "aircraft": rows}


def shape_polar(raw: dict) -> dict:
    return {"buckets": [_build(b, (("bearing_deg", "bearing", "i"), ("max_nm", "max_dist_nm", "n")))
                        for b in raw.get("buckets") or [] if isinstance(b, dict)]}


def _https(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 500:
        return None
    if not value.startswith("https://") or any(c.isspace() or ord(c) < 0x20 for c in value):
        return None
    return value


def shape_photo(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {"photo": None}
    out: dict = {}
    for key in ("thumbnail_url", "large_url", "link_url"):
        _put(out, key, _https(raw.get(key)))
    if not out:
        return {"photo": None}
    _put(out, "photographer", _text(raw.get("photographer")))
    out["is_type_photo"] = bool(raw.get("is_type_photo"))
    if out["is_type_photo"]:
        _put(out, "type_code", _text(raw.get("type_code"), CODE_LIMIT))
    return out


def watchlist_entry(e: Any) -> dict:
    e = e if isinstance(e, dict) else {}
    out = _build(e, (("id", "id", "i"), ("match_type", "match_type", "c"),
                     ("value", "value", "t"), ("label", "label", "t"),
                     ("created_at", "created_at", "iso")))
    if e.get("airborne") is not None:
        out["airborne"] = bool(e["airborne"])
    return out


# ---- final passes -----------------------------------------------------------
def strip_location(obj: Any) -> Any:
    """Recursively drop receiver coordinates and flight bounding boxes."""
    if isinstance(obj, dict):
        return {k: strip_location(v) for k, v in obj.items() if k not in _LOCATION_KEYS}
    if isinstance(obj, list):
        return [strip_location(v) for v in obj]
    return obj


def compact_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _size(obj: Any) -> int:
    return len(compact_json(obj).encode("utf-8"))


def _longest_list(obj: Any) -> list | None:
    best: list | None = None
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            if cur and (best is None or len(cur) > len(best)):
                best = cur
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return best


def cap_size(payload: dict, max_bytes: int | None = None) -> dict:
    """Trim the longest list(s) until the compact JSON fits; mark ``truncated``."""
    max_bytes = MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
    if _size(payload) <= max_bytes:
        return payload
    payload["truncated"] = True
    while _size(payload) > max_bytes:
        lst = _longest_list(payload)
        if lst is None:
            stub = {"truncated": True, "error": "response too large"}
            return stub
        del lst[len(lst) * 3 // 4:]
    return payload
