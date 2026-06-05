"""Flight list, detail, positions, chart, export, photo endpoints."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from .. import config, downsample, enrichment, schemas
from . import _deps, _photos


router = APIRouter()


@router.get("/api/flights")
def api_flights(
    date: str | None = Query(None, description="YYYY-MM-DD (receiver local time)"),
    date_from: str | None = Query(None, description="YYYY-MM-DD inclusive range start (receiver local time)"),
    date_to:   str | None = Query(None, description="YYYY-MM-DD inclusive range end (receiver local time)"),
    from_ts: int | None = Query(None, alias="from", description="Unix timestamp range start (browser-local midnight)"),
    to_ts:   int | None = Query(None, alias="to",   description="Unix timestamp range end (browser-local midnight of next day)"),
    icao: str | None = Query(None),
    callsign: str | None = Query(None),
    registration: str | None = Query(None),
    aircraft_type: str | None = Query(None),
    source: str | None = Query(None, description="adsb | mlat | mixed | other"),
    flags: str | None = Query(None, description="military | interesting | anonymous"),
    squawk: str | None = Query(None),
    has_acars: bool | None = Query(None, description="only flights with VDL2/ACARS (opt-in)"),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
) -> dict:
    conn = _deps.db()
    # Only surface the VDL2 `has_acars` column/filter when vdl2.db is actually
    # attached (RSBS_VDL2_ENABLED + file present). Otherwise the param is ignored
    # and the query is identical to the no-VDL2 build. Re-attempt the attach per
    # request (idempotent) so a vdl2.db that appears AFTER this thread's core
    # connection was created still gets picked up without a web restart.
    _deps._maybe_attach_vdl2(conn)
    show_acars = _deps.vdl2_attached(conn)
    where, params = _deps._build_flight_filter(
        date, icao, callsign, registration, aircraft_type, source, flags, squawk,
        date_from=date_from, date_to=date_to, from_ts=from_ts, to_ts=to_ts,
        has_acars=(True if (has_acars and show_acars) else None),
    )

    # Only JOIN extra tables for COUNT when filters need them
    needs_join = registration or aircraft_type or flags
    count_join = _deps._FLIGHT_JOIN if needs_join else ""
    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM flights f {count_join} {where}", params
    ).fetchone()
    total = total_row["n"] if total_row else 0

    sort_col = _deps._SORT_COLS.get(sort_by or "", "f.first_seen")
    sort_order = "ASC" if sort_dir == "asc" else "DESC"

    acars_col = f", {_deps._HAS_ACARS_EXPR}" if show_acars else ""
    rows = conn.execute(
        f"""
        SELECT {_deps._FLIGHT_COLS}{acars_col}
        FROM flights f {_deps._FLIGHT_JOIN}
        {where}
        ORDER BY {sort_col} {sort_order}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "flights": [dict(r) for r in rows],
    }


@router.get("/api/flights/export.csv")
def api_flights_export(
    date: str | None = Query(None, description="YYYY-MM-DD (receiver local time)"),
    date_from: str | None = Query(None, description="YYYY-MM-DD inclusive range start (receiver local time)"),
    date_to:   str | None = Query(None, description="YYYY-MM-DD inclusive range end (receiver local time)"),
    from_ts: int | None = Query(None, alias="from",
        description="Unix timestamp range start (browser-local midnight)"),
    to_ts:   int | None = Query(None, alias="to",
        description="Unix timestamp range end (browser-local midnight of next day)"),
    icao: str | None = Query(None),
    callsign: str | None = Query(None),
    registration: str | None = Query(None),
    aircraft_type: str | None = Query(None),
    source: str | None = Query(None, description="adsb | mlat | mixed | other"),
    flags: str | None = Query(None, description="military | interesting | anonymous"),
    squawk: str | None = Query(None),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
) -> Response:
    # Audit 2026-05-26: accept the same epoch from/to params the History
    # page sends to /api/flights. Without these the export ignored the
    # visible date range and produced an unfiltered CSV.
    where, params = _deps._build_flight_filter(
        date, icao, callsign, registration, aircraft_type, source, flags, squawk,
        date_from=date_from, date_to=date_to, from_ts=from_ts, to_ts=to_ts,
    )
    sort_col = _deps._SORT_COLS.get(sort_by or "", "f.first_seen")
    sort_order = "ASC" if sort_dir == "asc" else "DESC"

    # Audit-13 A13-055: stream rows instead of materialising the entire
    # CSV in memory. On a Pi 4 a 50k-row export previously buffered a
    # multi-MB StringIO before the first byte hit the wire.
    sql = f"""
        SELECT {_deps._FLIGHT_COLS}
        FROM flights f {_deps._FLIGHT_JOIN}
        {where}
        ORDER BY {sort_col} {sort_order}
        LIMIT ?
        """
    bind = params + [config.MAX_EXPORT_ROWS]
    conn = _deps.db()

    def _iter_csv():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_deps._CSV_COLS)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        cursor = conn.execute(sql, bind)
        while True:
            chunk = cursor.fetchmany(1000)
            if not chunk:
                break
            for r in chunk:
                writer.writerow([
                    _deps._fmt_ts(r["first_seen"]), _deps._fmt_ts(r["last_seen"]), r["duration_sec"],
                    r["icao_hex"], r["callsign"] or "", r["registration"] or "",
                    r["aircraft_type"] or "", r["type_desc"] or "",
                    r["squawk"] or "", r["category"] or "", r["primary_source"] or "",
                    r["max_alt_baro"], r["max_gs"],
                    round(r["max_distance_nm"], 1) if r["max_distance_nm"] is not None else "",
                    r["total_positions"], r["adsb_positions"], r["mlat_positions"],
                    r["origin_icao"] or "", r["dest_icao"] or "",
                ])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    filename = f"flights_{date}.csv" if date else "flights.csv"
    return StreamingResponse(
        _iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/flights/{flight_id}", response_model=schemas.FlightDetailResponse,
            response_model_exclude_unset=True)
def api_flight_detail(
    flight_id: int,
    include_positions: bool = Query(False),
) -> dict:
    conn = _deps.db()
    flight = conn.execute(
        f"SELECT {_deps._FLIGHT_COLS} FROM flights f {_deps._FLIGHT_JOIN} WHERE f.id = ?",
        (flight_id,),
    ).fetchone()
    if flight is None:
        raise HTTPException(404, "Flight not found")

    # BE-10: the raw position timeline is no longer embedded by default —
    # the SPA pulls it from /positions (paginated) and /positions/chart
    # (downsampled). `include_positions=true` restores the full embed for
    # any non-frontend consumer.
    positions = []
    if include_positions:
        positions = conn.execute(
            """
            SELECT ts, lat, lon, alt_baro, alt_geom, gs, track,
                   baro_rate, rssi, source_type
            FROM positions
            WHERE flight_id = ?
            ORDER BY ts
            """,
            (flight_id,),
        ).fetchall()

    # Enrich callsign → airline name
    flight_dict = dict(flight)
    flight_dict["airline_name"] = enrichment.lookup_airline(conn, flight_dict.get("callsign"))

    other_flights = conn.execute(
        f"""
        SELECT {_deps._FLIGHT_COLS}
        FROM flights f {_deps._FLIGHT_JOIN}
        WHERE f.icao_hex = ? AND f.id != ?
        ORDER BY f.first_seen DESC
        LIMIT 10
        """,
        (flight["icao_hex"], flight_id),
    ).fetchall()

    return {
        "flight": flight_dict,
        "positions": [dict(p) for p in positions],
        "other_flights": [dict(f) for f in other_flights],
        "receiver_lat": config.RECEIVER_LAT,
        "receiver_lon": config.RECEIVER_LON,
    }


@router.get("/api/flights/{flight_id}/positions",
            response_model=schemas.FlightPositionsResponse,
            response_model_exclude_unset=True)
def api_flight_positions(
    flight_id: int,
    limit: int = Query(_deps._POSITIONS_DEFAULT_LIMIT, ge=1, le=_deps._POSITIONS_MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict:
    """Paginated raw positions for inspection. Uses the new
    ``idx_positions_flight_ts`` composite (3a) for the
    ``WHERE flight_id = ? ORDER BY ts LIMIT ? OFFSET ?`` pattern."""
    conn = _deps.db()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE flight_id = ?",
        (flight_id,),
    ).fetchone()["n"]
    rows = conn.execute(
        """
        SELECT ts, lat, lon, alt_baro, alt_geom, gs, track,
               baro_rate, rssi, source_type
        FROM positions
        WHERE flight_id = ?
        ORDER BY ts
        LIMIT ? OFFSET ?
        """,
        (flight_id, limit, offset),
    ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "positions": [dict(r) for r in rows],
    }


@router.get("/api/flights/{flight_id}/positions/chart",
            response_model=schemas.FlightPositionsChartResponse,
            response_model_exclude_unset=True)
def api_flight_positions_chart(
    flight_id: int,
    target: int = Query(_deps._CHART_DEFAULT_TARGET, ge=2, le=_deps._CHART_MAX_TARGET),
) -> dict:
    """LTTB-downsampled positions for chart/map rendering.

    Picks ``target`` rows from the full positions stream using altitude
    as the LTTB signal (falling back to ground speed when altitude is
    NULL — common on positions before climbout). The same row picks
    drive every parallel series in the response so the chart, map
    polyline, and any future overlay stay row-aligned.
    """
    conn = _deps.db()
    rows = conn.execute(
        """
        SELECT ts, lat, lon, alt_baro, alt_geom, gs, track,
               baro_rate, source_type
        FROM positions
        WHERE flight_id = ?
        ORDER BY ts
        """,
        (flight_id,),
    ).fetchall()

    if not rows:
        return {"total": 0, "target": target, "positions": []}

    # Build (ts, signal) tuples for LTTB. Prefer alt_baro; fall back to
    # gs so ground/taxi rows still contribute. Last-ditch zero keeps the
    # index well-defined when both are NULL.
    signal: list[tuple[float, float]] = []
    for r in rows:
        ts = r["ts"]
        y = r["alt_baro"]
        if y is None:
            y = r["gs"]
        if y is None:
            y = 0.0
        signal.append((float(ts), float(y)))

    indices = downsample.lttb_indices(signal, target)
    picked = [dict(rows[i]) for i in indices]
    return {
        "total": len(rows),
        "target": target,
        "positions": picked,
    }


@router.get("/api/flights/{flight_id}/photo",
            response_model=schemas.PhotoResponse | None,
            response_model_exclude_unset=True)
async def api_flight_photo(flight_id: int) -> dict | None:
    row = _deps.db().execute(
        """
        SELECT f.icao_hex,
               COALESCE(f.aircraft_type, adb.type_code, axo.type_code) AS type_code,
               COALESCE(adb.type_desc, axo.type_desc, f.aircraft_type) AS type_desc
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        WHERE f.id = ?
        """,
        (flight_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404)
    specific = await _photos._fetch_photo(row["icao_hex"])
    if specific:
        return _photos._annotate_photo(specific, is_type=False)
    type_photo = await _photos._fetch_type_photo(row["type_code"])
    return _photos._annotate_photo(type_photo, is_type=True,
                                   type_code=row["type_code"], type_desc=row["type_desc"])
