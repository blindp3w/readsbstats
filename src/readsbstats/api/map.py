"""Heatmap, coverage, live, and historical map-snapshot endpoints.

Also owns ``_compute_heatmap_sync`` and ``_compute_coverage_sync`` — the
heavy aggregation functions imported lazily by ``cache._prewarm_one`` to
avoid a ``cache → api.map → cache`` import cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Query

from .. import analytics, cache, config, geo, schemas
from . import _deps


log = logging.getLogger("web")
router = APIRouter()


def _compute_heatmap_sync(window: str) -> dict:
    """Run the heavy aggregation query — call via run_in_executor to avoid blocking the event loop.

    Tries the DuckDB engine first (gated by `analytics.is_available()`);
    falls through to a SQLite query on unavailable or per-query failure.
    Both paths feed into shared post-processing so the response shape is
    identical."""
    precision = _deps._HEATMAP_PRECISION[window]
    secs = _deps._HEATMAP_WINDOWS[window]
    cutoff = (int(time.time()) - secs) if secs is not None else None

    try:
        rows: list[tuple[float, float, int]] | None = analytics.heatmap(cutoff, precision)
    except Exception:  # noqa: BLE001 — belt-and-suspenders: SQLite path must still answer
        log.warning("analytics.heatmap raised; falling back to SQLite", exc_info=True)
        rows = None
    if rows is None:
        params: list = []
        extra = ""
        if cutoff is not None:
            extra = "AND ts > ?"
            params.append(cutoff)
        # improvements.md A13-019: GROUP BY integer bucket (FLOOR(x*10^p + 0.5))
        # and divide in Python so this path agrees bucket-for-bucket with the
        # DuckDB heatmap.  Raw `round()` differs across engines (SQLite is
        # half-away-from-zero, DuckDB is banker's) and even an explicit
        # `FLOOR/scale` on the SQL side picks up a per-engine float drift on
        # the divide step.
        scale = 10 ** precision
        sqlite_rows = _deps.db().execute(
            f"""
            SELECT CAST(FLOOR(lat * {scale} + 0.5) AS INTEGER) AS lat_bucket,
                   CAST(FLOOR(lon * {scale} + 0.5) AS INTEGER) AS lon_bucket,
                   COUNT(*) AS w
            FROM positions
            WHERE lat IS NOT NULL AND lon IS NOT NULL
              {extra}
            GROUP BY lat_bucket, lon_bucket
            """,
            params,
        ).fetchall()
        rows = [(r["lat_bucket"] / scale, r["lon_bucket"] / scale, r["w"]) for r in sqlite_rows]

    if not rows:
        return {"points": [], "window": window, "count": 0}

    max_w = max(r[2] for r in rows)
    return {
        "points": [[r[0], r[1], r[2] / max_w] for r in rows],
        "window": window,
        "count": sum(r[2] for r in rows),
    }


@router.get("/api/map/heatmap")
async def api_map_heatmap(window: str = Query("7d")) -> dict:
    """Return position density grid for Leaflet.heat overlay.

    Intensities are normalised so the densest cell = 1.0.
    Fine grid (0.01°) for 24h/7d; coarse grid (0.1°) for 30d/all.
    """
    if window not in _deps._HEATMAP_WINDOWS:
        raise HTTPException(
            400, f"window must be one of: {', '.join(_deps._HEATMAP_WINDOWS)}"
        )

    cache_key = f"heatmap:{window}"
    cached = cache._get_cache(cache_key)
    if cached is not None:
        return cached

    async with cache._heatmap_lock(window):
        cached = cache._get_cache(cache_key)
        if cached is not None:
            return cached
        result = await asyncio.get_running_loop().run_in_executor(
            None, _compute_heatmap_sync, window
        )
        cache._set_cache(cache_key, result)
        return result


def _compute_coverage_sync(window: str) -> dict:
    """Compute per-bearing max-range polygon from raw positions — call via run_in_executor.

    Bearing and haversine distance are computed per-position in SQL so each 10° bucket
    reflects the actual farthest position recorded in that direction, not just the single
    furthest-point bearing stored on the flight row.  DuckDB engine first (when available);
    SQLite fallback on unavailable or per-query failure.
    """
    secs = _deps._HEATMAP_WINDOWS[window]
    cutoff = (int(time.time()) - secs) if secs is not None else None

    try:
        by_bucket = analytics.coverage(cutoff, config.RECEIVER_LAT, config.RECEIVER_LON, _deps._BUCKET_DEG)
    except Exception:  # noqa: BLE001
        log.warning("analytics.coverage raised; falling back to SQLite", exc_info=True)
        by_bucket = None
    if by_bucket is None:
        params: dict = {"rlat": config.RECEIVER_LAT, "rlon": config.RECEIVER_LON}
        extra = ""
        if cutoff is not None:
            extra = "AND ts > :cutoff"
            params["cutoff"] = cutoff

        # Audit-13 A13-076: shared SQL helpers — single source of truth.
        bearing_expr = geo.bearing_sql("lat", "lon", ":rlat", ":rlon")
        dist_expr    = geo.haversine_sql("lat", "lon", ":rlat", ":rlon")
        rows = _deps.db().execute(
            f"""
            WITH pos_bearing AS (
                SELECT
                    {bearing_expr} AS bearing_deg,
                    {dist_expr}    AS dist_nm
                FROM positions
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  {extra}
            )
            SELECT
                CAST(bearing_deg / {_deps._BUCKET_DEG}.0 AS INT) % {_deps._NUM_BUCKETS} AS bucket,
                MAX(dist_nm) AS max_dist
            FROM pos_bearing
            GROUP BY bucket
            """,
            params,
        ).fetchall()
        by_bucket = {r["bucket"]: r["max_dist"] for r in rows}

    polygon: list[list[float]] = []
    for i in range(_deps._NUM_BUCKETS):
        dist = by_bucket.get(i, 0.0)
        if dist > 0:
            lat, lon = geo.destination_point(
                config.RECEIVER_LAT, config.RECEIVER_LON, float(i * _deps._BUCKET_DEG), dist
            )
        else:
            lat, lon = config.RECEIVER_LAT, config.RECEIVER_LON
        polygon.append([lat, lon])

    max_range = max(by_bucket.values(), default=0.0)
    return {"polygon": polygon, "max_range_nm": max_range, "window": window}


@router.get("/api/map/coverage")
async def api_map_coverage(window: str = Query("7d")) -> dict:
    """Return receiver coverage polygon for Leaflet overlay.

    Each of 36 bearing buckets (10° each) contains the max detection range
    in that direction, projected to a lat/lon point.  Buckets with no data
    collapse to the receiver location, pulling the polygon inward.
    """
    if window not in _deps._HEATMAP_WINDOWS:
        raise HTTPException(
            400, f"window must be one of: {', '.join(_deps._HEATMAP_WINDOWS)}"
        )

    cache_key = f"coverage:{window}"
    cached = cache._get_cache(cache_key)
    if cached is not None:
        return cached

    async with cache._coverage_lock(window):
        cached = cache._get_cache(cache_key)
        if cached is not None:
            return cached
        result = await asyncio.get_running_loop().run_in_executor(
            None, _compute_coverage_sync, window
        )
        cache._set_cache(cache_key, result)
        return result


@router.get("/api/live")
def api_live() -> dict:
    """
    Audit-13 A13-069: single query (was two — fetch IDs, then bind into an
    IN-clause). The correlated subquery uses idx_positions_flight_id_desc
    on (flight_id, id DESC), so each per-flight position lookup is
    O(log n) without materialising a Python list of active IDs.
    """
    conn = _deps.db()
    rows = conn.execute(
        f"""
        SELECT af.icao_hex, af.flight_id, af.last_seen,
               f.callsign,
               COALESCE(f.registration, adb.registration, axo.registration) AS registration,
               COALESCE(f.aircraft_type, adb.type_code, axo.type_code)     AS aircraft_type,
               {_deps._FLAGS_EXPR_AF}                                      AS flags,
               f.primary_source,
               cr.origin_icao,
               cr.dest_icao,
               p.lat,
               p.lon
        FROM active_flights af
        JOIN flights f ON f.id = af.flight_id
        LEFT JOIN aircraft_db      adb ON adb.icao_hex  = af.icao_hex
        LEFT JOIN adsbx_overrides  axo ON axo.icao_hex  = af.icao_hex
        LEFT JOIN callsign_routes  cr  ON cr.callsign   = f.callsign
        LEFT JOIN positions p ON p.id = (
            SELECT id FROM positions
            WHERE flight_id = af.flight_id
              AND lat IS NOT NULL
              AND lon IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
        )
        ORDER BY af.last_seen DESC
        """
    ).fetchall()
    now = int(time.time())
    aircraft = []
    for r in rows:
        d = dict(r)
        d["seconds_ago"] = now - r["last_seen"]
        aircraft.append(d)
    return {
        "now": now,
        "count": len(rows),
        "receiver_lat": config.RECEIVER_LAT,
        "receiver_lon": config.RECEIVER_LON,
        "aircraft": aircraft,
    }


@router.get("/api/map/snapshot", response_model=schemas.MapSnapshotResponse,
            response_model_exclude_unset=True)
def api_map_snapshot(
    at: int | None = Query(None, description="Unix timestamp (default: now → live mode)"),
    trail: int = Query(10, ge=0, description="Trail positions per aircraft (capped at 50)"),
) -> dict:
    now = int(time.time())

    if at is None:
        at = now
        is_live = True
    else:
        if at > now + 60:
            raise HTTPException(400, "at timestamp cannot be in the future")
        oldest = now - config.MAP_HISTORY_HOURS * 3600
        if at < oldest:
            raise HTTPException(
                400,
                f"at timestamp exceeds history limit ({config.MAP_HISTORY_HOURS}h)",
            )
        is_live = abs(at - now) <= 30

    trail_count = min(trail, 50)
    conn = _deps.db()

    rows = conn.execute(
        f"""
        WITH af AS (
            SELECT id, flight_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY flight_id ORDER BY ts DESC, id DESC
                   ) AS rn
            FROM positions
            WHERE ts BETWEEN ? AND ?
              AND lat IS NOT NULL AND lon IS NOT NULL
        )
        SELECT p.flight_id, p.ts, p.lat, p.lon, p.alt_baro, p.gs, p.track,
               p.source_type,
               f.icao_hex, f.callsign,
               {_deps._ENRICH_REG} AS registration,
               {_deps._ENRICH_TYPE} AS aircraft_type,
               f.category, f.primary_source,
               {_deps._FLAGS_EXPR_F} AS flags,
               cr.origin_icao, cr.dest_icao
        FROM af
        JOIN positions p ON p.id = af.id
        JOIN flights f ON f.id = p.flight_id
        {_deps._ENRICH_JOIN}
        LEFT JOIN callsign_routes cr  ON cr.callsign  = f.callsign
        WHERE af.rn = 1
        """,
        (at - _deps._MAP_WINDOW_SEC, at),
    ).fetchall()

    aircraft = []
    for r in rows:
        d = dict(r)
        d["seconds_ago"] = at - r["ts"]
        aircraft.append(d)

    if trail_count > 0 and aircraft:
        flight_ids = [r["flight_id"] for r in aircraft]
        placeholders = ",".join("?" * len(flight_ids))
        # PY-11 (Audit 2026-05-31): time-bound the live-view trail CTE so
        # a long flight with thousands of historical positions doesn't
        # force SQLite to rank the whole partition just to return
        # `trail_count` points. RSBS_MAP_TRAIL_WINDOW_SECONDS controls
        # the reach; default 3600s comfortably exceeds the 600s
        # live-view activity window.
        #
        # Historical replay (is_live=False) skips the lower bound — the
        # user is reviewing past activity and expects to see the whole
        # flight track up to `at`, not just the last hour of it.
        # `trail_count` (capped at 50) is itself a cap on partition
        # materialisation, so historical replay can't pathologically
        # scan more than 50 × |flight_ids| rows.
        if is_live:
            trail_lo_sql = "AND ts >= ?"
            trail_lo_params = [at - config.MAP_TRAIL_WINDOW_SECONDS]
        else:
            trail_lo_sql = ""
            trail_lo_params = []
        trail_rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT flight_id, ts, lat, lon,
                       ROW_NUMBER() OVER (PARTITION BY flight_id ORDER BY ts DESC) AS rn
                FROM positions
                WHERE flight_id IN ({placeholders})
                  AND ts <= ?
                  {trail_lo_sql}
                  AND lat IS NOT NULL AND lon IS NOT NULL
            )
            SELECT flight_id, ts, lat, lon FROM ranked WHERE rn <= ?
            ORDER BY flight_id, ts
            """,
            [*flight_ids, at, *trail_lo_params, trail_count],
        ).fetchall()

        trail_by_flight: dict[int, list] = {}
        for tr in trail_rows:
            fid = tr["flight_id"]
            if fid not in trail_by_flight:
                trail_by_flight[fid] = []
            trail_by_flight[fid].append([tr["lat"], tr["lon"], tr["ts"]])

        for ac in aircraft:
            ac["trail"] = trail_by_flight.get(ac["flight_id"], [])
    else:
        for ac in aircraft:
            ac["trail"] = []

    return {
        "at":           at,
        "is_live":      is_live,
        "receiver_lat": config.RECEIVER_LAT,
        "receiver_lon": config.RECEIVER_LON,
        "aircraft":     aircraft,
    }
