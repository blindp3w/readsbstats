"""Aircraft drill-down, flagged gallery, airline/type flights, and aircraft-level photo."""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import config, icao_ranges, photo_sources, schemas
from . import _deps, _photos


router = APIRouter()


@router.get("/api/aircraft/{icao_hex}/flights")
def api_aircraft_flights(
    icao_hex: str,
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
) -> dict:
    conn = _deps.db()
    icao = _deps._parse_icao_path(icao_hex)

    # Single query for count + aggregates (avoids two full scans)
    aggs_row = conn.execute(
        """
        SELECT COUNT(*)                    AS total_flights,
               MIN(first_seen)             AS first_seen,
               MAX(last_seen)              AS last_seen,
               SUM(last_seen - first_seen) AS total_duration_sec
        FROM flights WHERE icao_hex = ?
        """,
        (icao,),
    ).fetchone()
    total = aggs_row["total_flights"] if aggs_row else 0

    order_col = _deps._SORT_COLS.get(sort_by or "first_seen", "f.first_seen")
    order_dir = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"

    rows = conn.execute(
        f"""
        SELECT {_deps._FLIGHT_COLS}
        FROM flights f {_deps._FLIGHT_JOIN}
        WHERE f.icao_hex = ?
        ORDER BY {order_col} {order_dir}
        LIMIT ? OFFSET ?
        """,
        (icao, limit, offset),
    ).fetchall()

    adb_row = conn.execute(
        "SELECT registration, type_code, type_desc, flags FROM aircraft_db WHERE icao_hex = ?",
        (icao,),
    ).fetchone()
    aircraft_info = {**(dict(adb_row) if adb_row else {}), **(dict(aggs_row) if aggs_row else {})}
    aircraft_info["country"] = icao_ranges.icao_to_country(icao)

    return {"total": total, "icao_hex": icao, "aircraft_info": aircraft_info,
            "flights": [dict(r) for r in rows]}


@router.get("/api/aircraft/flagged")
def api_aircraft_flagged(
    flags: str | None = Query(None, description="military | interesting | anonymous"),
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
) -> dict:
    conn = _deps.db()

    flag_expr = _deps._FLAGS_EXPR_F
    if flags == "military":
        flag_filter = f"({flag_expr} & 1) = 1"
    elif flags == "interesting":
        flag_filter = f"({flag_expr} & 2) = 2 AND ({flag_expr} & 1) = 0"
    elif flags == "anonymous":
        # Same precedence rule as the history filter — surface anon-only here.
        flag_filter = f"({flag_expr} & 16) = 16 AND ({flag_expr} & 3) = 0"
    else:
        # Default "all" tab: military | interesting | anonymous
        flag_filter = (
            f"({flag_expr} & {config.FLAG_MILITARY | config.FLAG_INTERESTING | config.FLAG_ANONYMOUS}) != 0"
        )

    # Audit-13 A13-077: shared allowlist at module top so this endpoint
    # follows the same pattern as /api/flights — no inline ad-hoc maps.
    order_col = _deps._FLAGGED_SORT_COLS.get(sort_by or "last_seen", "last_seen")
    order_dir = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"

    base_joins = """
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
    """

    total = conn.execute(
        f"SELECT COUNT(DISTINCT f.icao_hex) AS cnt {base_joins} WHERE {flag_filter}"
    ).fetchone()["cnt"]

    # BE-15 (Audit 2026-05-31): `base` picks one deterministic representative
    # flight per ICAO (latest by last_seen, id as tiebreak) via a window
    # function; `agg` carries the COUNT/MIN/MAX aggregates.  This replaces the
    # old GROUP BY whose bare reg/type columns came from an arbitrary grouped
    # row.  The window runs only over the flag-filtered (small) set, so the
    # extra sort is bounded on the Pi-4.
    rows = conn.execute(
        f"""
        WITH base AS (
            SELECT
                f.icao_hex,
                COALESCE(f.registration, adb.registration, axo.registration)  AS registration,
                COALESCE(f.aircraft_type, adb.type_code, axo.type_code)       AS aircraft_type,
                COALESCE(adb.type_desc, axo.type_desc, '')                    AS type_desc,
                COALESCE(adb.type_code, axo.type_code, f.aircraft_type)       AS tp_type,
                {flag_expr}                                                   AS flags,
                ROW_NUMBER() OVER (
                    PARTITION BY f.icao_hex ORDER BY f.last_seen DESC, f.id DESC
                )                                                             AS rn
            {base_joins}
            WHERE {flag_filter}
        ),
        agg AS (
            SELECT f.icao_hex,
                   COUNT(*)          AS flight_count,
                   MIN(f.first_seen) AS first_seen,
                   MAX(f.last_seen)  AS last_seen
            {base_joins}
            WHERE {flag_filter}
            GROUP BY f.icao_hex
        )
        SELECT
            b.icao_hex,
            b.registration,
            b.aircraft_type,
            b.type_desc,
            b.flags,
            a.flight_count,
            a.first_seen,
            a.last_seen,
            COALESCE(p.thumbnail_url, tp.thumbnail_url)                     AS thumbnail_url,
            COALESCE(p.large_url,     tp.large_url)                         AS large_url,
            COALESCE(p.link_url,      tp.link_url)                          AS link_url,
            COALESCE(p.photographer,  tp.photographer)                      AS photographer,
            CASE WHEN p.thumbnail_url IS NULL AND tp.thumbnail_url IS NOT NULL
                 THEN 1 ELSE 0 END                                          AS is_type_photo
        FROM base b
        JOIN agg a ON a.icao_hex = b.icao_hex
        LEFT JOIN photos p      ON p.icao_hex  = b.icao_hex
        LEFT JOIN type_photos tp ON tp.type_code = b.tp_type
        WHERE b.rn = 1
        ORDER BY {order_col} {order_dir}
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()

    aircraft = []
    for r in rows:
        d = dict(r)
        d["is_type_photo"] = bool(d["is_type_photo"])
        d["country"] = icao_ranges.icao_to_country(d["icao_hex"])
        # PY-6 (Audit 2026-05-31): the photo columns come straight from
        # `photos` / `type_photos` (no per-source allowlist gate at SELECT
        # time). Apply the API-boundary suppression so off-allowlist
        # cached URLs don't reach the SPA, regardless of _HOST_ENFORCE.
        if not photo_sources.is_photo_url_allowed(d.get("thumbnail_url")):
            d["thumbnail_url"] = None
            d["large_url"]     = None
            d["link_url"]      = None
            d["photographer"]  = None
            d["is_type_photo"] = False
        else:
            for field in ("large_url", "link_url"):
                if not photo_sources.is_photo_url_allowed(d.get(field)):
                    d[field] = None
        aircraft.append(d)

    return {"total": total, "aircraft": aircraft}


@router.get("/api/aircraft/{icao_hex}/photo",
            response_model=schemas.PhotoResponse | None,
            response_model_exclude_unset=True)
async def api_aircraft_photo(icao_hex: str) -> dict | None:
    icao = _deps._parse_icao_path(icao_hex)
    row = _deps.db().execute(
        """
        SELECT COALESCE(adb.type_code, axo.type_code) AS type_code,
               COALESCE(adb.type_desc, axo.type_desc) AS type_desc
        FROM (SELECT ? AS h) base
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = base.h
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = base.h
        """,
        (icao,),
    ).fetchone()
    type_code = row["type_code"] if row else None
    type_desc = row["type_desc"] if row else None
    specific = await _photos._fetch_photo(icao)
    if specific:
        return _photos._annotate_photo(specific, is_type=False)
    type_photo = await _photos._fetch_type_photo(type_code)
    return _photos._annotate_photo(type_photo, is_type=True, type_code=type_code, type_desc=type_desc)


@router.get("/api/airlines/{prefix}/flights")
def api_airline_flights(
    prefix: str,
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
) -> dict:
    conn = _deps.db()
    p = prefix.upper()[:3]
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM flights f WHERE f.callsign LIKE ?",
        (p + "%",),
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT {_deps._FLIGHT_COLS}
        FROM flights f {_deps._FLIGHT_JOIN}
        WHERE f.callsign LIKE ?
        ORDER BY f.first_seen DESC
        LIMIT ? OFFSET ?
        """,
        (p + "%", limit, offset),
    ).fetchall()
    return {"total": total, "airline": p, "flights": [dict(r) for r in rows]}


@router.get("/api/types/{aircraft_type}/flights")
def api_type_flights(
    aircraft_type: str,
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
) -> dict:
    conn = _deps.db()
    t = aircraft_type.upper()
    # PY-2 (Audit 2026-05-31): COUNT needs to match the list query — both
    # must include adsbx_overrides so a flight whose type is only known
    # via adsbx still shows up here.
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM flights f "
        "LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex "
        "LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex "
        f"WHERE {_deps._ENRICH_TYPE} = ?",
        (t,),
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT {_deps._FLIGHT_COLS}
        FROM flights f {_deps._FLIGHT_JOIN}
        WHERE {_deps._ENRICH_TYPE} = ?
        ORDER BY f.first_seen DESC
        LIMIT ? OFFSET ?
        """,
        (t, limit, offset),
    ).fetchall()
    return {"total": total, "aircraft_type": t, "flights": [dict(r) for r in rows]}
