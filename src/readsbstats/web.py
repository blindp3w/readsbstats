"""
readsbstats — FastAPI web server.

Serves the flight history web UI and JSON API.
Run via uvicorn (see systemd/readsbstats-web.service).
"""

import asyncio
import csv
import io
import json
import logging
import math
import os
import sqlite3
import sys
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import config, database, enrichment, geo, icao_ranges, route_enricher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("web")

BASE_DIR = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Geometry helpers (used by polar endpoint)
# ---------------------------------------------------------------------------

_haversine_nm = geo.haversine_nm
_bearing = geo.bearing

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app_: FastAPI) -> AsyncIterator[None]:
    log.info("Starting web server — DB: %s", config.DB_PATH)
    database._migrate(db())
    route_enricher.start_background_enricher()
    yield
    log.info("Web server stopped")


app = FastAPI(root_path=config.ROOT_PATH, docs_url=None, redoc_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["root_path"] = config.ROOT_PATH

# ---------------------------------------------------------------------------
# DB connection — one per process (WAL allows concurrent readers)
# ---------------------------------------------------------------------------
_db: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = database.connect()
    return _db


# ---------------------------------------------------------------------------
# Response cache — simple TTL dict, keyed by endpoint name
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTLS: dict[str, int] = {
    "stats":   120,   # seconds — aggregate data, no need to recompute often
    "polar":   300,   # seconds — max range rarely shifts
    "records": 300,   # seconds — all-time bests, very stable
}
_DEFAULT_TTL  = 30    # seconds
_AIRSPACE_TTL = 3600  # seconds — airspace data rarely changes


def _get_cache(key: str) -> object | None:
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < _CACHE_TTLS.get(key, _DEFAULT_TTL):
        return entry[1]
    return None


def _set_cache(key: str, value: object) -> None:
    _cache[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _fmt_ts(epoch: int | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


templates.env.filters["fmt_ts"] = _fmt_ts


# ---------------------------------------------------------------------------
# Enrichment helper — resolves reg/type via aircraft_db when NULL in flights
# ---------------------------------------------------------------------------

# Joined SELECT fragment used in flight list and detail queries
_FLIGHT_COLS = """
    f.id,
    f.icao_hex,
    f.callsign                                            AS callsign,
    COALESCE(f.registration,  adb.registration, axo.registration)  AS registration,
    COALESCE(f.aircraft_type, adb.type_code,    axo.type_code)     AS aircraft_type,
    COALESCE(adb.type_desc,   axo.type_desc,    '')                AS type_desc,
    (COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0))             AS flags,
    f.squawk,
    f.category,
    f.primary_source,
    f.first_seen,
    f.last_seen,
    (f.last_seen - f.first_seen)                         AS duration_sec,
    f.max_alt_baro,
    f.max_gs,
    f.max_distance_nm,
    f.total_positions,
    f.adsb_positions,
    f.mlat_positions,
    f.lat_min, f.lat_max, f.lon_min, f.lon_max,
    f.origin_icao,
    f.dest_icao,
    ap_orig.name     AS origin_name,
    ap_orig.country  AS origin_country,
    ap_dest.name     AS dest_name,
    ap_dest.country  AS dest_country
"""

_FLIGHT_JOIN = """
    LEFT JOIN aircraft_db      adb  ON adb.icao_hex     = f.icao_hex
    LEFT JOIN adsbx_overrides  axo  ON axo.icao_hex     = f.icao_hex
    LEFT JOIN airports         ap_orig ON ap_orig.icao_code = f.origin_icao
    LEFT JOIN airports         ap_dest ON ap_dest.icao_code = f.dest_icao
"""

# Whitelist of sortable columns for /api/flights
_SORT_COLS: dict[str, str] = {
    "first_seen":     "f.first_seen",
    "icao_hex":       "f.icao_hex",
    "callsign":       "f.callsign",
    "registration":   "COALESCE(f.registration, adb.registration)",
    "aircraft_type":  "COALESCE(f.aircraft_type, adb.type_code)",
    "primary_source": "f.primary_source",
    "duration_sec":   "(f.last_seen - f.first_seen)",
    "max_alt_baro":   "f.max_alt_baro",
    "max_gs":         "f.max_gs",
    "max_distance_nm":"f.max_distance_nm",
    "total_positions":"f.total_positions",
    "origin_icao":    "f.origin_icao",
    "dest_icao":      "f.dest_icao",
}


# ---------------------------------------------------------------------------
# Receiver metrics — column allowlist and aggregation types
# ---------------------------------------------------------------------------

_METRICS_COLS = frozenset({
    "ac_with_pos", "ac_without_pos", "ac_adsb", "ac_mlat",
    "signal", "noise", "peak_signal", "strong_signals",
    "local_modes", "local_bad", "local_unknown_icao",
    "local_accepted_0", "local_accepted_1",
    "samples_dropped", "samples_lost",
    "messages", "positions_total", "positions_adsb", "positions_mlat",
    "max_distance_m", "tracks_new", "tracks_single",
    "cpu_demod", "cpu_reader", "cpu_background", "cpu_aircraft_json", "cpu_heatmap",
    "remote_modes", "remote_bad", "remote_accepted", "remote_bytes_in", "remote_bytes_out",
    "cpr_airborne", "cpr_global_ok", "cpr_global_bad", "cpr_global_range",
    "cpr_global_speed", "cpr_global_skipped", "cpr_local_ok",
    "cpr_local_range", "cpr_local_speed", "cpr_filtered",
    "altitude_suppressed",
})

# Columns where MAX is the correct aggregation (peaks / extremes)
_METRICS_MAX = frozenset({"peak_signal", "strong_signals", "max_distance_m"})
# Columns where AVG is the correct aggregation (continuous measurements)
_METRICS_AVG = frozenset({
    "ac_with_pos", "ac_without_pos", "ac_adsb", "ac_mlat",
    "signal", "noise",
    "cpu_demod", "cpu_reader", "cpu_background", "cpu_aircraft_json", "cpu_heatmap",
    "samples_dropped", "samples_lost",
})
# Everything else uses SUM (counters per interval)


def _metrics_agg(col: str) -> str:
    """Return the SQL aggregate function for a metrics column."""
    if col in _METRICS_MAX:
        return f"MAX({col})"
    if col in _METRICS_AVG:
        return f"AVG({col})"
    return f"SUM({col})"


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def page_stats(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "stats.html")


@app.get("/history", response_class=HTMLResponse)
async def page_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.get("/flight/{flight_id}", response_class=HTMLResponse)
async def page_flight(request: Request, flight_id: int) -> HTMLResponse:
    return templates.TemplateResponse(request, "flight.html", {"flight_id": flight_id})


@app.get("/aircraft/{icao_hex}", response_class=HTMLResponse)
async def page_aircraft(request: Request, icao_hex: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "aircraft.html",
        {"icao_hex": icao_hex.lower().lstrip("~")})


@app.get("/live", response_class=HTMLResponse)
async def page_live(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "live.html")


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request) -> HTMLResponse:
    tok_masked = "configured" if config.TELEGRAM_TOKEN else "not set"
    cid_masked = "configured" if config.TELEGRAM_CHAT_ID else "not set"
    return templates.TemplateResponse(request, "settings.html", {
        # Receiver
        "lat":              config.RECEIVER_LAT,
        "lon":              config.RECEIVER_LON,
        "max_range":        config.RECEIVER_MAX_RANGE,
        # Collector
        "poll_interval":    config.POLL_INTERVAL_SEC,
        "flight_gap":       config.FLIGHT_GAP_SEC,
        "min_positions":    config.MIN_POSITIONS_KEEP,
        "max_seen_pos":     config.MAX_SEEN_POS_SEC,
        "max_speed_kts":    config.MAX_SPEED_KTS,
        # Database
        "db_path":          config.DB_PATH,
        "retention_days":   config.RETENTION_DAYS,
        "purge_interval":   config.PURGE_INTERVAL_SEC,
        # Enrichment
        "photo_cache_days":    config.PHOTO_CACHE_DAYS,
        "airspace_geojson":    config.AIRSPACE_GEOJSON or "(bundled poland.geojson)",
        "route_cache_days":    config.ROUTE_CACHE_DAYS,
        "route_interval":      config.ROUTE_ENRICH_INTERVAL,
        "route_batch":         config.ROUTE_BATCH_SIZE,
        "route_rate_limit":    config.ROUTE_RATE_LIMIT_SEC,
        # External ADS-B enrichment
        "adsbx_enabled":       config.ADSBX_ENABLED,
        "adsbx_interval":      config.ADSBX_POLL_INTERVAL,
        "adsbx_range":         config.ADSBX_RANGE_NM,
        "adsbx_url":           config.ADSBX_API_URL,
        # Receiver metrics
        "metrics_enabled":     config.METRICS_ENABLED,
        "metrics_interval":    config.METRICS_INTERVAL,
        "stats_json":          config.STATS_JSON,
        # Web server
        "web_host":         config.WEB_HOST,
        "web_port":         config.WEB_PORT,
        "root_path":        config.ROOT_PATH,
        # UI
        "page_size":        config.DEFAULT_PAGE_SIZE,
        "max_page_size":    config.MAX_PAGE_SIZE,
        # Telegram
        "telegram_token":       tok_masked,
        "telegram_chat_id":     cid_masked,
        "telegram_summary_time": config.TELEGRAM_SUMMARY_TIME,
        "telegram_units":       config.TELEGRAM_UNITS,
        "base_url":             config.BASE_URL,
    })


@app.get("/watchlist", response_class=HTMLResponse)
async def page_watchlist(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "watchlist.html")


@app.get("/gallery", response_class=HTMLResponse)
async def page_gallery(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "gallery.html")


@app.get("/metrics", response_class=HTMLResponse)
async def page_metrics(request: Request) -> HTMLResponse:
    has_data = db().execute(
        "SELECT 1 FROM receiver_stats LIMIT 1"
    ).fetchone() is not None
    return templates.TemplateResponse(request, "metrics.html", {
        "has_data": has_data,
    })


# ---------------------------------------------------------------------------
# API — watchlist
# ---------------------------------------------------------------------------

_VALID_MATCH_TYPES = {"icao", "registration", "callsign_prefix"}


class _WatchlistEntry(BaseModel):
    match_type: str
    value: str
    label: str | None = None


@app.get("/api/watchlist")
async def api_watchlist_list() -> dict:
    rows = db().execute(
        """
        SELECT w.id, w.match_type, w.value, w.label, w.created_at,
               CASE WHEN w.match_type = 'icao' AND af.icao_hex IS NOT NULL
                    THEN 1 ELSE 0 END AS airborne
        FROM watchlist w
        LEFT JOIN active_flights af ON w.match_type = 'icao' AND af.icao_hex = w.value
        ORDER BY w.created_at
        """
    ).fetchall()
    return {"entries": [dict(r) for r in rows]}


@app.post("/api/watchlist", status_code=201)
async def api_watchlist_add(body: _WatchlistEntry) -> dict:
    if body.match_type not in _VALID_MATCH_TYPES:
        raise HTTPException(422, "match_type must be icao, registration, or callsign_prefix")
    value = body.value.strip().lower()
    if not value:
        raise HTTPException(422, "value is required")
    label = body.label.strip() if body.label else None
    try:
        with db():
            cur = db().execute(
                "INSERT INTO watchlist (match_type, value, label, created_at) "
                "VALUES (?, ?, ?, strftime('%s','now'))",
                (body.match_type, value, label),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Entry already exists")
    return {"id": cur.lastrowid, "match_type": body.match_type, "value": value, "label": label}


@app.delete("/api/watchlist/{entry_id}", status_code=204)
async def api_watchlist_delete(entry_id: int) -> Response:
    row = db().execute("SELECT id FROM watchlist WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    with db():
        db().execute("DELETE FROM watchlist WHERE id = ?", (entry_id,))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# API — flight list
# ---------------------------------------------------------------------------

def _build_flight_filter(
    date: str | None,
    icao: str | None,
    callsign: str | None,
    registration: str | None,
    aircraft_type: str | None,
    source: str | None,
    flags: str | None,
    squawk: str | None = None,
) -> tuple[str, list]:
    """Return (WHERE clause, params list) for the shared flight filter params."""
    conditions: list[str] = []
    params: list = []

    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD")
        day_start = int(day.timestamp())
        day_end = day_start + 86400
        conditions.append("f.first_seen >= ? AND f.first_seen < ?")
        params += [day_start, day_end]

    if icao:
        conditions.append("f.icao_hex = ?")
        params.append(icao.lower().lstrip("~"))

    if callsign:
        conditions.append("f.callsign LIKE ?")
        params.append(callsign.upper().strip() + "%")

    if registration:
        conditions.append("COALESCE(f.registration, adb.registration) LIKE ?")
        params.append(registration.upper().strip() + "%")

    if aircraft_type:
        conditions.append("COALESCE(f.aircraft_type, adb.type_code) = ?")
        params.append(aircraft_type.upper().strip())

    if source:
        conditions.append("f.primary_source = ?")
        params.append(source.lower())

    if flags == "military":
        conditions.append("((COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 1) = 1")
    elif flags == "interesting":
        conditions.append(
            "((COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 2) = 2"
            " AND ((COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 1) = 0"
        )

    if squawk:
        conditions.append("f.squawk = ?")
        params.append(squawk.strip())

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


@app.get("/api/flights")
async def api_flights(
    date: str | None = Query(None, description="YYYY-MM-DD (UTC)"),
    icao: str | None = Query(None),
    callsign: str | None = Query(None),
    registration: str | None = Query(None),
    aircraft_type: str | None = Query(None),
    source: str | None = Query(None, description="adsb | mlat | mixed | other"),
    flags: str | None = Query(None, description="military | interesting"),
    squawk: str | None = Query(None),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
) -> dict:
    where, params = _build_flight_filter(date, icao, callsign, registration, aircraft_type, source, flags, squawk)
    conn = db()

    # Only JOIN extra tables for COUNT when filters need them
    needs_join = registration or aircraft_type or flags
    count_join = _FLIGHT_JOIN if needs_join else ""
    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM flights f {count_join} {where}", params
    ).fetchone()
    total = total_row["n"] if total_row else 0

    sort_col = _SORT_COLS.get(sort_by or "", "f.first_seen")
    sort_order = "ASC" if sort_dir == "asc" else "DESC"

    rows = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
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


_CSV_COLS = [
    "first_seen", "last_seen", "duration_sec",
    "icao_hex", "callsign", "registration", "aircraft_type", "type_desc",
    "squawk", "category", "primary_source",
    "max_alt_baro", "max_gs", "max_distance_nm",
    "total_positions", "adsb_positions", "mlat_positions",
    "origin_icao", "dest_icao",
]


@app.get("/api/flights/export.csv")
async def api_flights_export(
    date: str | None = Query(None, description="YYYY-MM-DD (UTC)"),
    icao: str | None = Query(None),
    callsign: str | None = Query(None),
    registration: str | None = Query(None),
    aircraft_type: str | None = Query(None),
    source: str | None = Query(None, description="adsb | mlat | mixed | other"),
    flags: str | None = Query(None, description="military | interesting"),
    squawk: str | None = Query(None),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
) -> Response:
    where, params = _build_flight_filter(date, icao, callsign, registration, aircraft_type, source, flags, squawk)
    sort_col = _SORT_COLS.get(sort_by or "", "f.first_seen")
    sort_order = "ASC" if sort_dir == "asc" else "DESC"

    rows = db().execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        {where}
        ORDER BY {sort_col} {sort_order}
        LIMIT ?
        """,
        params + [config.MAX_EXPORT_ROWS],
    ).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLS)
    for r in rows:
        writer.writerow([
            _fmt_ts(r["first_seen"]), _fmt_ts(r["last_seen"]), r["duration_sec"],
            r["icao_hex"], r["callsign"] or "", r["registration"] or "",
            r["aircraft_type"] or "", r["type_desc"] or "",
            r["squawk"] or "", r["category"] or "", r["primary_source"] or "",
            r["max_alt_baro"], r["max_gs"],
            round(r["max_distance_nm"], 1) if r["max_distance_nm"] is not None else "",
            r["total_positions"], r["adsb_positions"], r["mlat_positions"],
            r["origin_icao"] or "", r["dest_icao"] or "",
        ])

    filename = f"flights_{date}.csv" if date else "flights.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# API — single flight detail
# ---------------------------------------------------------------------------

@app.get("/api/flights/{flight_id}")
async def api_flight_detail(flight_id: int) -> dict:
    conn = db()
    flight = conn.execute(
        f"SELECT {_FLIGHT_COLS} FROM flights f {_FLIGHT_JOIN} WHERE f.id = ?",
        (flight_id,),
    ).fetchone()
    if flight is None:
        raise HTTPException(404, "Flight not found")

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
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
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


# ---------------------------------------------------------------------------
# API — aircraft photo (Planespotters → airport-data.com → hexdb.io, cached)
# ---------------------------------------------------------------------------

_PHOTO_UA = {"User-Agent": "readsbstats/1.0"}


async def _fetch_photo(icao_hex: str) -> dict | None:
    """Try Planespotters.net, airport-data.com, then hexdb.io.  Returns result dict or None."""
    conn = db()

    # Serve from cache if fresh
    cached = conn.execute(
        "SELECT * FROM photos WHERE icao_hex = ? AND fetched_at > ?",
        (icao_hex, int(time.time()) - config.PHOTO_CACHE_DAYS * 86400),
    ).fetchone()
    if cached:
        return dict(cached) if cached["thumbnail_url"] else None

    result = None

    # --- Source 1: Planespotters.net ---
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(
                f"https://api.planespotters.net/pub/photos/hex/{icao_hex}",
                headers=_PHOTO_UA,
            )
            resp.raise_for_status()
            photos = resp.json().get("photos", [])
            if photos:
                p = photos[0]
                result = {
                    "icao_hex":      icao_hex,
                    "thumbnail_url": p.get("thumbnail", {}).get("src"),
                    "large_url":     p.get("thumbnail_large", {}).get("src"),
                    "link_url":      p.get("link"),
                    "photographer":  p.get("photographer"),
                }
    except Exception:
        log.exception("Planespotters photo fetch failed for %s", icao_hex)

    # --- Source 2: airport-data.com fallback ---
    if result is None:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(
                    f"https://airport-data.com/api/ac_thumb.json?m={icao_hex}&n=1",
                    headers=_PHOTO_UA,
                )
                resp.raise_for_status()
                ad = resp.json()
                if ad.get("status") == 200 and ad.get("data"):
                    item = ad["data"][0]
                    result = {
                        "icao_hex":      icao_hex,
                        "thumbnail_url": item.get("image"),
                        "large_url":     item.get("image"),
                        "link_url":      item.get("link"),
                        "photographer":  item.get("photographer"),
                    }
        except Exception:
            log.exception("airport-data.com photo fetch failed for %s", icao_hex)

    # --- Source 3: hexdb.io fallback ---
    if result is None:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(
                    f"https://hexdb.io/hex-image?hex={icao_hex}",
                    headers=_PHOTO_UA,
                )
                resp.raise_for_status()
                url = resp.text.strip()
                if url and url != "n/a":
                    result = {
                        "icao_hex":      icao_hex,
                        "thumbnail_url": url,
                        "large_url":     url,
                        "link_url":      None,
                        "photographer":  None,
                    }
        except Exception:
            log.exception("hexdb.io photo fetch failed for %s", icao_hex)

    # --- Cache result (including NULL for "no photo anywhere") ---
    now = int(time.time())
    if result:
        result["fetched_at"] = now
        conn.execute(
            "INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?)",
            (icao_hex, result["thumbnail_url"], result["large_url"],
             result["link_url"], result["photographer"], now),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO photos "
            "(icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES (?,NULL,NULL,NULL,NULL,?)",
            (icao_hex, now),
        )
    conn.commit()
    return result


@app.get("/api/flights/{flight_id}/photo")
async def api_flight_photo(flight_id: int) -> dict | None:
    row = db().execute("SELECT icao_hex FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return await _fetch_photo(row["icao_hex"])


# ---------------------------------------------------------------------------
# API — aircraft history
# ---------------------------------------------------------------------------

@app.get("/api/aircraft/{icao_hex}/flights")
async def api_aircraft_flights(
    icao_hex: str,
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
) -> dict:
    conn = db()
    icao = icao_hex.lower().lstrip("~")

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

    order_col = _SORT_COLS.get(sort_by or "first_seen", "f.first_seen")
    order_dir = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"

    rows = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
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


# ---------------------------------------------------------------------------
# API — flagged aircraft gallery (military + interesting)
# ---------------------------------------------------------------------------

@app.get("/api/aircraft/flagged")
async def api_aircraft_flagged(
    flags: str | None = Query(None, description="military | interesting"),
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
) -> dict:
    conn = db()

    flag_expr = "(COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0))"
    if flags == "military":
        flag_filter = f"({flag_expr} & 1) = 1"
    elif flags == "interesting":
        flag_filter = f"({flag_expr} & 2) = 2 AND ({flag_expr} & 1) = 0"
    else:
        flag_filter = f"({flag_expr} & 3) != 0"

    sort_map = {
        "last_seen": "last_seen",
        "first_seen": "first_seen",
        "flight_count": "flight_count",
        "registration": "registration",
        "aircraft_type": "aircraft_type",
    }
    order_col = sort_map.get(sort_by or "last_seen", "last_seen")
    order_dir = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"

    base_joins = """
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
    """

    total = conn.execute(
        f"SELECT COUNT(DISTINCT f.icao_hex) AS cnt {base_joins} WHERE {flag_filter}"
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT
            f.icao_hex,
            COALESCE(f.registration, adb.registration, axo.registration) AS registration,
            COALESCE(f.aircraft_type, adb.type_code, axo.type_code)      AS aircraft_type,
            COALESCE(adb.type_desc, axo.type_desc, '')                   AS type_desc,
            {flag_expr}                                                  AS flags,
            COUNT(*)                                                     AS flight_count,
            MIN(f.first_seen)                                            AS first_seen,
            MAX(f.last_seen)                                             AS last_seen,
            p.thumbnail_url,
            p.large_url,
            p.link_url,
            p.photographer
        {base_joins}
        LEFT JOIN photos p ON p.icao_hex = f.icao_hex
        WHERE {flag_filter}
        GROUP BY f.icao_hex
        ORDER BY {order_col} {order_dir}
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()

    aircraft = []
    for r in rows:
        d = dict(r)
        d["country"] = icao_ranges.icao_to_country(d["icao_hex"])
        aircraft.append(d)

    return {"total": total, "aircraft": aircraft}


@app.get("/api/aircraft/{icao_hex}/photo")
async def api_aircraft_photo(icao_hex: str) -> dict | None:
    return await _fetch_photo(icao_hex.lower().lstrip("~"))


# ---------------------------------------------------------------------------
# API — statistics
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def api_stats(
    from_ts: int | None = Query(None, alias="from"),
    to_ts:   int | None = Query(None, alias="to"),
) -> dict:
    filtered = from_ts is not None or to_ts is not None
    cache_key = "stats" if not filtered else f"stats:{from_ts}:{to_ts}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    conn = db()
    now = int(time.time())
    cutoff_24h      = now - 86400
    cutoff_7d       = now - 7 * 86400
    cutoff_30d      = now - 30 * 86400
    cutoff_prev_24h = now - 2 * 86400
    cutoff_prev_7d  = now - 14 * 86400

    # WHERE fragments injected into all range-aware queries
    if filtered:
        ts_lo = from_ts if from_ts is not None else 0
        ts_hi = to_ts   if to_ts   is not None else now
        _fw   = "WHERE first_seen >= ? AND first_seen <= ?"    # standalone
        _fwa  = "AND first_seen >= ? AND first_seen <= ?"      # appended to existing WHERE
        _fjwa = "AND f.first_seen >= ? AND f.first_seen <= ?"  # appended, aliased table
        _fp   = (ts_lo, ts_hi)
    else:
        _fw = _fwa = _fjwa = ""
        _fp = ()

    # --- Main aggregation ---
    if not filtered:
        # Single pass including relative-window CASE WHENs
        agg = conn.execute(
            """
            SELECT
                COUNT(*)                                                          AS total_flights,
                SUM(total_positions)                                              AS total_positions,
                COUNT(DISTINCT icao_hex)                                          AS unique_aircraft,
                COUNT(DISTINCT CASE
                    WHEN callsign IS NOT NULL AND callsign != '' AND length(callsign) >= 3
                    THEN substr(callsign,1,3) END)                                AS unique_airlines,
                MIN(first_seen)                                                   AS oldest_flight,
                SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END)                  AS flights_24h,
                SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END)                  AS flights_7d,
                SUM(CASE WHEN first_seen > ? AND first_seen <= ? THEN 1 ELSE 0 END) AS flights_prev_24h,
                SUM(CASE WHEN first_seen > ? AND first_seen <= ? THEN 1 ELSE 0 END) AS flights_prev_7d,
                ROUND(100.0 * SUM(adsb_positions) / NULLIF(SUM(total_positions),0), 1) AS adsb_pct,
                ROUND(100.0 * SUM(mlat_positions) / NULLIF(SUM(total_positions),0), 1) AS mlat_pct,
                SUM(CASE WHEN max_alt_baro >= 0     AND max_alt_baro < 1000  THEN 1 ELSE 0 END) AS alt_0_1k,
                SUM(CASE WHEN max_alt_baro >= 1000  AND max_alt_baro < 5000  THEN 1 ELSE 0 END) AS alt_1k_5k,
                SUM(CASE WHEN max_alt_baro >= 5000  AND max_alt_baro < 10000 THEN 1 ELSE 0 END) AS alt_5k_10k,
                SUM(CASE WHEN max_alt_baro >= 10000 AND max_alt_baro < 20000 THEN 1 ELSE 0 END) AS alt_10k_20k,
                SUM(CASE WHEN max_alt_baro >= 20000 AND max_alt_baro < 30000 THEN 1 ELSE 0 END) AS alt_20k_30k,
                SUM(CASE WHEN max_alt_baro >= 30000 AND max_alt_baro < 40000 THEN 1 ELSE 0 END) AS alt_30k_40k,
                SUM(CASE WHEN max_alt_baro >= 40000 THEN 1 ELSE 0 END)           AS alt_40k_plus,
                SUM(CASE WHEN squawk = '7700' THEN 1 ELSE 0 END)                 AS squawk_7700,
                SUM(CASE WHEN squawk = '7600' THEN 1 ELSE 0 END)                 AS squawk_7600,
                SUM(CASE WHEN squawk = '7500' THEN 1 ELSE 0 END)                 AS squawk_7500
            FROM flights
            """,
            (cutoff_24h, cutoff_7d, cutoff_prev_24h, cutoff_24h, cutoff_prev_7d, cutoff_7d),
        ).fetchone()
        flights_24h      = agg["flights_24h"]
        flights_7d       = agg["flights_7d"]
        flights_prev_24h = agg["flights_prev_24h"] or 0
        flights_prev_7d  = agg["flights_prev_7d"]  or 0
    else:
        # Filtered: agg over selected range; live window stats from separate query
        agg = conn.execute(
            f"""
            SELECT
                COUNT(*)                                                          AS total_flights,
                SUM(total_positions)                                              AS total_positions,
                COUNT(DISTINCT icao_hex)                                          AS unique_aircraft,
                COUNT(DISTINCT CASE
                    WHEN callsign IS NOT NULL AND callsign != '' AND length(callsign) >= 3
                    THEN substr(callsign,1,3) END)                                AS unique_airlines,
                MIN(first_seen)                                                   AS oldest_flight,
                ROUND(100.0 * SUM(adsb_positions) / NULLIF(SUM(total_positions),0), 1) AS adsb_pct,
                ROUND(100.0 * SUM(mlat_positions) / NULLIF(SUM(total_positions),0), 1) AS mlat_pct,
                SUM(CASE WHEN max_alt_baro >= 0     AND max_alt_baro < 1000  THEN 1 ELSE 0 END) AS alt_0_1k,
                SUM(CASE WHEN max_alt_baro >= 1000  AND max_alt_baro < 5000  THEN 1 ELSE 0 END) AS alt_1k_5k,
                SUM(CASE WHEN max_alt_baro >= 5000  AND max_alt_baro < 10000 THEN 1 ELSE 0 END) AS alt_5k_10k,
                SUM(CASE WHEN max_alt_baro >= 10000 AND max_alt_baro < 20000 THEN 1 ELSE 0 END) AS alt_10k_20k,
                SUM(CASE WHEN max_alt_baro >= 20000 AND max_alt_baro < 30000 THEN 1 ELSE 0 END) AS alt_20k_30k,
                SUM(CASE WHEN max_alt_baro >= 30000 AND max_alt_baro < 40000 THEN 1 ELSE 0 END) AS alt_30k_40k,
                SUM(CASE WHEN max_alt_baro >= 40000 THEN 1 ELSE 0 END)           AS alt_40k_plus,
                SUM(CASE WHEN squawk = '7700' THEN 1 ELSE 0 END)                 AS squawk_7700,
                SUM(CASE WHEN squawk = '7600' THEN 1 ELSE 0 END)                 AS squawk_7600,
                SUM(CASE WHEN squawk = '7500' THEN 1 ELSE 0 END)                 AS squawk_7500
            FROM flights {_fw}
            """,
            _fp,
        ).fetchone()
        live = conn.execute(
            """
            SELECT
                SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END)                     AS flights_24h,
                SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END)                     AS flights_7d,
                SUM(CASE WHEN first_seen > ? AND first_seen <= ? THEN 1 ELSE 0 END) AS flights_prev_24h,
                SUM(CASE WHEN first_seen > ? AND first_seen <= ? THEN 1 ELSE 0 END) AS flights_prev_7d
            FROM flights
            """,
            (cutoff_24h, cutoff_7d, cutoff_prev_24h, cutoff_24h, cutoff_prev_7d, cutoff_7d),
        ).fetchone()
        flights_24h      = live["flights_24h"]
        flights_7d       = live["flights_7d"]
        flights_prev_24h = live["flights_prev_24h"] or 0
        flights_prev_7d  = live["flights_prev_7d"]  or 0

    adsb_pct  = agg["adsb_pct"]  or 0
    mlat_pct  = agg["mlat_pct"]  or 0
    other_pct = round(100.0 - adsb_pct - mlat_pct, 1)

    try:
        db_size = os.path.getsize(config.DB_PATH)
    except OSError:
        db_size = None

    # Military + interesting — one JOIN pass (OR-merge tar1090-db + ADSBx flags)
    flags_row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN ((COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 1) = 1 THEN 1 ELSE 0 END) AS military,
            SUM(CASE WHEN ((COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 2) = 2
                      AND ((COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 1) = 0 THEN 1 ELSE 0 END) AS interesting
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        {"WHERE f.first_seen >= ? AND f.first_seen <= ?" if filtered else ""}
        """,
        _fp,
    ).fetchone()

    # Top airlines
    top_airlines = conn.execute(
        f"""
        SELECT
            substr(f.callsign,1,3)      AS airline,
            al.name                     AS airline_name,
            COUNT(*)                    AS flights,
            COUNT(DISTINCT f.icao_hex)  AS unique_aircraft
        FROM flights f
        LEFT JOIN airlines al ON al.icao_code = substr(f.callsign,1,3)
        WHERE f.callsign IS NOT NULL AND length(f.callsign) >= 3 {_fjwa}
        GROUP BY airline
        ORDER BY flights DESC
        LIMIT 20
        """,
        _fp,
    ).fetchall()

    # Top aircraft types
    top_types = conn.execute(
        f"""
        SELECT
            COALESCE(f.aircraft_type, adb.type_code)  AS type,
            COALESCE(adb.type_desc, '')                AS type_desc,
            COUNT(*)                                   AS flights,
            COUNT(DISTINCT f.icao_hex)                 AS unique_aircraft
        FROM flights f
        LEFT JOIN aircraft_db adb ON adb.icao_hex = f.icao_hex
        WHERE COALESCE(f.aircraft_type, adb.type_code) IS NOT NULL {_fjwa}
        GROUP BY type
        ORDER BY flights DESC
        LIMIT 20
        """,
        _fp,
    ).fetchall()

    # Top routes (origin → destination, by flight count)
    top_routes = conn.execute(
        f"""
        SELECT cr.origin_icao,
               cr.dest_icao,
               ap_o.name   AS origin_name,
               ap_d.name   AS dest_name,
               COUNT(*)    AS flights
        FROM flights f
        JOIN callsign_routes cr ON cr.callsign = f.callsign
        LEFT JOIN airports ap_o ON ap_o.icao_code = cr.origin_icao
        LEFT JOIN airports ap_d ON ap_d.icao_code = cr.dest_icao
        WHERE cr.origin_icao IS NOT NULL AND cr.dest_icao IS NOT NULL {_fjwa}
        GROUP BY cr.origin_icao, cr.dest_icao
        ORDER BY flights DESC
        LIMIT 20
        """,
        _fp,
    ).fetchall()

    # Top airports (combined origin + destination appearances)
    top_airports = conn.execute(
        f"""
        SELECT icao_code, name, country, SUM(cnt) AS appearances
        FROM (
            SELECT ap_o.icao_code, ap_o.name, ap_o.country, COUNT(*) AS cnt
            FROM flights f
            JOIN callsign_routes cr ON cr.callsign = f.callsign
            JOIN airports ap_o ON ap_o.icao_code = cr.origin_icao
            WHERE cr.origin_icao IS NOT NULL {_fjwa}
            GROUP BY ap_o.icao_code
            UNION ALL
            SELECT ap_d.icao_code, ap_d.name, ap_d.country, COUNT(*) AS cnt
            FROM flights f
            JOIN callsign_routes cr ON cr.callsign = f.callsign
            JOIN airports ap_d ON ap_d.icao_code = cr.dest_icao
            WHERE cr.dest_icao IS NOT NULL {_fjwa}
            GROUP BY ap_d.icao_code
        )
        GROUP BY icao_code
        ORDER BY appearances DESC
        LIMIT 20
        """,
        _fp + _fp,
    ).fetchall()

    # Hourly distribution
    hourly = conn.execute(
        f"""
        SELECT CAST(strftime('%H', first_seen, 'unixepoch') AS INTEGER) AS hour,
               COUNT(*) AS count
        FROM flights {_fw}
        GROUP BY hour
        ORDER BY hour
        """,
        _fp,
    ).fetchall()
    hourly_map = {r["hour"]: r["count"] for r in hourly}
    hourly_dist = [{"hour": h, "count": hourly_map.get(h, 0)} for h in range(24)]

    # Daily unique aircraft
    if not filtered:
        daily = conn.execute(
            """
            SELECT date(first_seen, 'unixepoch') AS day,
                   COUNT(DISTINCT icao_hex) AS unique_aircraft,
                   COUNT(*) AS flights
            FROM flights
            WHERE first_seen > ?
            GROUP BY day
            ORDER BY day DESC
            LIMIT 30
            """,
            (cutoff_30d,),
        ).fetchall()
    else:
        daily = conn.execute(
            """
            SELECT date(first_seen, 'unixepoch') AS day,
                   COUNT(DISTINCT icao_hex) AS unique_aircraft,
                   COUNT(*) AS flights
            FROM flights
            WHERE first_seen >= ? AND first_seen <= ?
            GROUP BY day
            ORDER BY day ASC
            """,
            (ts_lo, ts_hi),
        ).fetchall()

    # New aircraft — first seen within the window
    if not filtered:
        new_having   = "HAVING MIN(f.first_seen) > ?"
        new_cnt_sql  = "SELECT COUNT(*) FROM (SELECT icao_hex FROM flights GROUP BY icao_hex HAVING MIN(first_seen) > ?)"
        new_params   = (cutoff_24h,)
    else:
        new_having   = "HAVING MIN(f.first_seen) >= ? AND MIN(f.first_seen) <= ?"
        new_cnt_sql  = "SELECT COUNT(*) FROM (SELECT icao_hex FROM flights GROUP BY icao_hex HAVING MIN(first_seen) >= ? AND MIN(first_seen) <= ?)"
        new_params   = (ts_lo, ts_hi)

    new_aircraft_rows = conn.execute(
        f"""
        SELECT sub.icao_hex,
               COALESCE(f2.registration, adb.registration, axo.registration) AS registration,
               COALESCE(f2.aircraft_type, adb.type_code, axo.type_code)     AS aircraft_type,
               COALESCE(adb.type_desc, axo.type_desc, '')                   AS type_desc,
               (COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0))           AS flags,
               sub.first_seen_ever
        FROM (
            SELECT f.icao_hex, MIN(f.first_seen) AS first_seen_ever
            FROM flights f
            GROUP BY f.icao_hex
            {new_having}
            ORDER BY MIN(f.first_seen) DESC
            LIMIT 10
        ) sub
        LEFT JOIN flights         f2  ON f2.icao_hex = sub.icao_hex AND f2.first_seen = sub.first_seen_ever
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = sub.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = sub.icao_hex
        ORDER BY sub.first_seen_ever DESC
        """,
        new_params,
    ).fetchall()
    new_aircraft_total = conn.execute(new_cnt_sql, new_params).fetchone()[0] or 0

    # Most frequent aircraft
    frequent_aircraft = conn.execute(
        f"""
        SELECT f.icao_hex,
               COALESCE(f.registration, adb.registration, axo.registration)  AS registration,
               COALESCE(f.aircraft_type, adb.type_code, axo.type_code)       AS aircraft_type,
               COALESCE(adb.type_desc, axo.type_desc, '')                    AS type_desc,
               (COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0))            AS flags,
               COUNT(*)                                    AS flights,
               MAX(f.last_seen)                            AS last_seen
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        {"WHERE f.first_seen >= ? AND f.first_seen <= ?" if filtered else ""}
        GROUP BY f.icao_hex
        ORDER BY flights DESC
        LIMIT 15
        """,
        _fp,
    ).fetchall()

    # Country breakdown — aggregated entirely in SQL using pre-built CASE expression
    top_countries = [dict(r) for r in conn.execute(
        f"""
        SELECT {icao_ranges.COUNTRY_SQL_CASE} AS country,
               COUNT(*)                        AS flights,
               COUNT(DISTINCT icao_hex)        AS unique_aircraft
        FROM flights {_fw}
        GROUP BY country
        ORDER BY flights DESC
        LIMIT 15
        """,
        _fp,
    ).fetchall()]

    # Activity heatmap
    heatmap_rows = conn.execute(
        f"""
        SELECT CAST(strftime('%w', first_seen, 'unixepoch') AS INT) AS dow,
               CAST(strftime('%H', first_seen, 'unixepoch') AS INT) AS hour,
               COUNT(*) AS count
        FROM flights {_fw}
        GROUP BY dow, hour
        ORDER BY dow, hour
        """,
        _fp,
    ).fetchall()

    # Furthest detected aircraft
    furthest_row = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        WHERE f.max_distance_nm IS NOT NULL {"AND f.first_seen >= ? AND f.first_seen <= ?" if filtered else ""}
        ORDER BY f.max_distance_nm DESC
        LIMIT 1
        """,
        _fp,
    ).fetchone()
    furthest = dict(furthest_row) if furthest_row else None

    result = {
        "total_flights":           agg["total_flights"],
        "total_positions":         agg["total_positions"],
        "unique_aircraft":         agg["unique_aircraft"],
        "unique_airlines":         agg["unique_airlines"],
        "db_size_bytes":           db_size,
        "oldest_flight":           agg["oldest_flight"],
        "flights_last_24h":        flights_24h,
        "flights_last_7d":         flights_7d,
        "source_breakdown":        {"adsb": adsb_pct, "mlat": mlat_pct, "other": other_pct},
        "top_airlines":            [dict(r) for r in top_airlines],
        "top_aircraft_types":      [dict(r) for r in top_types],
        "hourly_distribution":     hourly_dist,
        "daily_unique_aircraft":   [dict(r) for r in daily],
        "altitude_distribution":   [
            {"band": "Ground / <1k", "count": agg["alt_0_1k"]   or 0},
            {"band": "1k–5k",        "count": agg["alt_1k_5k"]  or 0},
            {"band": "5k–10k",       "count": agg["alt_5k_10k"] or 0},
            {"band": "10k–20k",      "count": agg["alt_10k_20k"] or 0},
            {"band": "20k–30k",      "count": agg["alt_20k_30k"] or 0},
            {"band": "30k–40k",      "count": agg["alt_30k_40k"] or 0},
            {"band": "40k+",         "count": agg["alt_40k_plus"] or 0},
        ],
        "military_flights":        flags_row["military"]     or 0,
        "interesting_flights":     flags_row["interesting"]  or 0,
        "squawk_counts":           {
            "7700": agg["squawk_7700"] or 0,
            "7600": agg["squawk_7600"] or 0,
            "7500": agg["squawk_7500"] or 0,
        },
        "new_aircraft":            {"total": new_aircraft_total, "items": [dict(r) for r in new_aircraft_rows]},
        "furthest_aircraft":       furthest,
        "receiver_lat":            config.RECEIVER_LAT,
        "receiver_lon":            config.RECEIVER_LON,
        "trends": {
            "flights_24h_prev": flights_prev_24h,
            "flights_7d_prev":  flights_prev_7d,
        },
        "heatmap": [dict(r) for r in heatmap_rows],
        "top_countries": top_countries,
        "frequent_aircraft": [dict(r) for r in frequent_aircraft],
        "top_routes":   [dict(r) for r in top_routes],
        "top_airports": [dict(r) for r in top_airports],
        "range":  {"from": from_ts, "to": to_ts} if filtered else None,
    }
    _set_cache(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# API — all-time personal records
# ---------------------------------------------------------------------------

@app.get("/api/stats/records")
async def api_stats_records() -> dict:
    """All-time personal records: furthest / fastest / highest / longest flight."""
    cached = _get_cache("records")
    if cached is not None:
        return cached

    conn = db()

    def _top1(order_col: str, extra_where: str = "") -> dict | None:
        row = conn.execute(
            f"""
            SELECT {_FLIGHT_COLS}
            FROM flights f {_FLIGHT_JOIN}
            WHERE f.{order_col} IS NOT NULL {extra_where}
            ORDER BY f.{order_col} DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    furthest = _top1("max_distance_nm")
    fastest  = _top1(
        "max_gs",
        f"AND f.max_gs <= CASE WHEN (COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 1 != 0"
        f" THEN {config.MAX_GS_MILITARY_KTS} ELSE {config.MAX_GS_CIVIL_KTS} END",
    )
    highest  = _top1("max_alt_baro")

    longest_row = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}, (f.last_seen - f.first_seen) AS duration_s
        FROM flights f {_FLIGHT_JOIN}
        WHERE f.last_seen > f.first_seen
        ORDER BY duration_s DESC
        LIMIT 1
        """
    ).fetchone()
    longest = dict(longest_row) if longest_row else None

    result = {
        "furthest": furthest,
        "fastest":  fastest,
        "highest":  highest,
        "longest":  longest,
    }
    _set_cache("records", result)
    return result


# ---------------------------------------------------------------------------
# API — airspace GeoJSON overlay
# ---------------------------------------------------------------------------

@app.get("/api/airspace")
async def api_airspace() -> dict:
    """Serve the configured airspace GeoJSON (default: bundled poland.geojson)."""
    cached = _cache.get("airspace")
    if cached and time.time() - cached[0] < _AIRSPACE_TTL:
        return cached[1]

    path = config.AIRSPACE_GEOJSON or str(BASE_DIR / "static" / "airspace" / "poland.geojson")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to load airspace from %s: %s", path, exc)
        data = {"type": "FeatureCollection", "features": []}

    _cache["airspace"] = (time.time(), data)
    return data


# ---------------------------------------------------------------------------
# API — polar range plot
# ---------------------------------------------------------------------------

@app.get("/api/stats/polar")
async def api_stats_polar() -> dict:
    """Max detection range per 10° azimuth bucket (36 buckets, 0 = North)."""
    cached = _get_cache("polar")
    if cached is not None:
        return cached

    # BUCKET_DEG controls angular resolution of the polar plot.
    # 10° (36 buckets) is a good default.
    BUCKET_DEG = 10
    n_buckets = 360 // BUCKET_DEG

    # Use precomputed max_distance_bearing from flights table (much faster
    # than scanning millions of position rows with trig math).
    sql_rows = db().execute(
        f"""
        SELECT
            CAST(max_distance_bearing / {BUCKET_DEG} AS INT) AS bucket,
            MAX(max_distance_nm) AS max_dist_nm
        FROM flights
        WHERE max_distance_nm IS NOT NULL AND max_distance_bearing IS NOT NULL
        GROUP BY bucket
        """
    ).fetchall()

    buckets: list[float] = [0.0] * n_buckets
    for r in sql_rows:
        if r["bucket"] is not None:
            buckets[int(r["bucket"]) % n_buckets] = r["max_dist_nm"] or 0.0

    result = {
        "buckets": [
            {"bearing": i * BUCKET_DEG, "max_dist_nm": round(buckets[i], 1)}
            for i in range(n_buckets)
        ]
    }
    _set_cache("polar", result)
    return result


# ---------------------------------------------------------------------------
# API — live (currently tracked aircraft)
# ---------------------------------------------------------------------------

@app.get("/api/live")
async def api_live() -> dict:
    conn = db()
    # Get active flight IDs first (small set), then fetch latest positions only for those
    active_ids = [r[0] for r in conn.execute("SELECT flight_id FROM active_flights").fetchall()]

    if active_ids:
        placeholders = ",".join("?" * len(active_ids))
        pos_rows = conn.execute(
            f"""
            SELECT flight_id, lat, lon
            FROM positions
            WHERE id IN (
                SELECT MAX(id) FROM positions
                WHERE flight_id IN ({placeholders})
                  AND lat IS NOT NULL AND lon IS NOT NULL
                GROUP BY flight_id
            )
            """,
            active_ids,
        ).fetchall()
        latest_pos = {r["flight_id"]: (r["lat"], r["lon"]) for r in pos_rows}
    else:
        latest_pos = {}

    rows = conn.execute(
        """
        SELECT af.icao_hex, af.flight_id, af.last_seen,
               f.callsign,
               COALESCE(f.registration, adb.registration, axo.registration) AS registration,
               COALESCE(f.aircraft_type, adb.type_code, axo.type_code)     AS aircraft_type,
               (COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0))          AS flags,
               f.primary_source,
               cr.origin_icao,
               cr.dest_icao
        FROM active_flights af
        JOIN flights f ON f.id = af.flight_id
        LEFT JOIN aircraft_db      adb ON adb.icao_hex  = af.icao_hex
        LEFT JOIN adsbx_overrides  axo ON axo.icao_hex  = af.icao_hex
        LEFT JOIN callsign_routes  cr  ON cr.callsign   = f.callsign
        ORDER BY af.last_seen DESC
        """
    ).fetchall()
    now = int(time.time())
    aircraft = []
    for r in rows:
        d = dict(r)
        d["seconds_ago"] = now - r["last_seen"]
        pos = latest_pos.get(r["flight_id"])
        d["lat"] = pos[0] if pos else None
        d["lon"] = pos[1] if pos else None
        aircraft.append(d)
    return {
        "now": now,
        "count": len(rows),
        "receiver_lat": config.RECEIVER_LAT,
        "receiver_lon": config.RECEIVER_LON,
        "aircraft": aircraft,
    }


# ---------------------------------------------------------------------------
# API — date index
# ---------------------------------------------------------------------------

@app.get("/api/dates")
async def api_dates() -> dict:
    conn = db()
    rows = conn.execute(
        """
        SELECT date(first_seen, 'unixepoch') AS date,
               COUNT(*) AS flight_count
        FROM flights
        GROUP BY date
        ORDER BY date DESC
        LIMIT 365
        """
    ).fetchall()
    return {"dates": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# API — airline and type drill-downs
# ---------------------------------------------------------------------------

@app.get("/api/airlines/{prefix}/flights")
async def api_airline_flights(
    prefix: str,
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
) -> dict:
    conn = db()
    p = prefix.upper()[:3]
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM flights f WHERE f.callsign LIKE ?",
        (p + "%",),
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        WHERE f.callsign LIKE ?
        ORDER BY f.first_seen DESC
        LIMIT ? OFFSET ?
        """,
        (p + "%", limit, offset),
    ).fetchall()
    return {"total": total, "airline": p, "flights": [dict(r) for r in rows]}


@app.get("/api/types/{aircraft_type}/flights")
async def api_type_flights(
    aircraft_type: str,
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
) -> dict:
    conn = db()
    t = aircraft_type.upper()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM flights f "
        "LEFT JOIN aircraft_db adb ON adb.icao_hex = f.icao_hex "
        "WHERE COALESCE(f.aircraft_type, adb.type_code) = ?",
        (t,),
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        WHERE COALESCE(f.aircraft_type, adb.type_code) = ?
        ORDER BY f.first_seen DESC
        LIMIT ? OFFSET ?
        """,
        (t, limit, offset),
    ).fetchall()
    return {"total": total, "aircraft_type": t, "flights": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# API — receiver metrics time-series
# ---------------------------------------------------------------------------

@app.get("/api/metrics")
async def api_metrics(
    request: Request,
    metrics: str = "signal,noise",
) -> dict:
    """
    Return receiver metrics as columnar arrays (uPlot-native format).

    Query params:
        from   — start epoch (default: 24 h ago)
        to     — end epoch (default: now)
        metrics — comma-separated column names from _METRICS_COLS
    """
    now = int(time.time())
    from_ts = int(request.query_params.get("from", now - 86400))
    to_ts = int(request.query_params.get("to", now))

    # Validate requested columns against allowlist
    requested = [c.strip() for c in metrics.split(",") if c.strip()]
    invalid = [c for c in requested if c not in _METRICS_COLS]
    if invalid:
        return JSONResponse(
            status_code=400,
            content={"error": f"unknown metrics: {', '.join(invalid)}"},
        )
    if not requested:
        return {"bucket_seconds": 0, "metrics": [], "data": []}

    # Auto-downsampling based on time range span
    span = to_ts - from_ts
    if span <= 86_400:          # <= 24 h: raw
        bucket = 0
    elif span <= 604_800:       # <= 7 d: 5-min buckets
        bucket = 300
    elif span <= 2_592_000:     # <= 30 d: 15-min buckets
        bucket = 900
    elif span <= 7_776_000:     # <= 90 d: 1-hour buckets
        bucket = 3600
    else:                       # > 90 d: 4-hour buckets
        bucket = 14400

    conn = db()
    if bucket == 0:
        cols_sql = ", ".join(requested)
        sql = (
            f"SELECT ts, {cols_sql} FROM receiver_stats "
            f"WHERE ts BETWEEN ? AND ? ORDER BY ts"
        )
        rows = conn.execute(sql, (from_ts, to_ts)).fetchall()
    else:
        agg_cols = ", ".join(_metrics_agg(c) for c in requested)
        sql = (
            f"SELECT (ts / {bucket}) * {bucket} AS bucket_ts, {agg_cols} "
            f"FROM receiver_stats WHERE ts BETWEEN ? AND ? "
            f"GROUP BY bucket_ts ORDER BY bucket_ts"
        )
        rows = conn.execute(sql, (from_ts, to_ts)).fetchall()

    # Build columnar arrays: [[ts, ...], [metric1, ...], [metric2, ...]]
    n_cols = 1 + len(requested)
    data: list[list] = [[] for _ in range(n_cols)]
    for row in rows:
        for i in range(n_cols):
            data[i].append(row[i])

    return {
        "bucket_seconds": bucket,
        "metrics": requested,
        "data": data,
    }


# ---------------------------------------------------------------------------
# API — health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def api_health() -> dict:
    try:
        db().execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db_path": config.DB_PATH}


# ---------------------------------------------------------------------------
# Feeders health page
# ---------------------------------------------------------------------------

async def _check_systemd_unit(unit: str) -> dict:
    """Run ``systemctl is-active <unit>`` and return the status string."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", unit,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        return {"systemd": stdout.decode().strip() or "unknown"}
    except FileNotFoundError:
        return {"systemd": "unavailable"}
    except asyncio.TimeoutError:
        return {"systemd": "timeout"}
    except Exception as exc:
        return {"systemd": f"error: {exc}"}


async def _check_port(port: int, host: str = "127.0.0.1") -> dict:
    """Try to open a TCP connection to *host*:*port*."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3.0,
        )
        writer.close()
        await writer.wait_closed()
        return {"port": port, "port_status": "open"}
    except (ConnectionRefusedError, OSError):
        return {"port": port, "port_status": "closed"}
    except asyncio.TimeoutError:
        return {"port": port, "port_status": "timeout"}


def _read_json_file(path: str) -> dict | None:
    """Read and parse a JSON file, returning None on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _feeder_details_readsb(status_path: str) -> list[tuple[str, str]]:
    """Extract key stats from a readsb JSON directory."""
    details: list[tuple[str, str]] = []
    ac = _read_json_file(f"{status_path}/aircraft.json")
    if ac:
        count = len(ac.get("aircraft", []))
        details.append(("Aircraft tracked", str(count)))
    stats = _read_json_file(f"{status_path}/stats.json")
    if stats:
        last = stats.get("last1min", {})
        msgs = last.get("messages", 0)
        if msgs:
            start = last.get("start", 0)
            end = last.get("end", 0)
            dur = end - start if end > start else 60
            details.append(("Messages/s", f"{msgs / dur:.0f}"))
        local = last.get("local", {})
        if "signal" in local:
            details.append(("Signal", f"{local['signal']:.1f} dBFS"))
        if "noise" in local:
            details.append(("Noise", f"{local['noise']:.1f} dBFS"))
        max_dist = last.get("max_distance")
        if max_dist:
            details.append(("Max range", f"{max_dist:.0f} nm"))
    return details


async def _feeder_details_fr24(status_url: str) -> list[tuple[str, str]]:
    """Fetch FR24 monitor.json and extract key fields."""
    details: list[tuple[str, str]] = []
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(status_url)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return details
    if data.get("build_version"):
        details.append(("Version", data["build_version"]))
    fs = data.get("feed_status")
    if fs:
        details.append(("FR24 link", fs))
    alias = data.get("feed_alias")
    if alias:
        details.append(("Radar code", alias))
    ac = data.get("feed_num_ac_tracked")
    if ac is not None:
        details.append(("Aircraft tracked", str(ac)))
    rx = data.get("rx_connected")
    if rx is not None:
        details.append(("Receiver", "connected" if str(rx) == "1" else "disconnected"))
    mlat_ok = data.get("mlat-ok")
    if mlat_ok is not None:
        details.append(("MLAT", "ok" if str(mlat_ok) == "1" else "not ok"))
    return details


def _feeder_details_piaware(status_path: str) -> list[tuple[str, str]]:
    """Read PiAware status.json and extract component statuses."""
    details: list[tuple[str, str]] = []
    data = _read_json_file(status_path)
    if not data:
        return details
    ver = data.get("piaware_version")
    if ver:
        details.append(("Version", f"PiAware {ver}"))
    for key in ("piaware", "adept", "radio", "mlat"):
        comp = data.get(key)
        if comp and isinstance(comp, dict):
            msg = comp.get("message", comp.get("status", ""))
            if msg:
                details.append((key.capitalize(), msg))
    cpu = data.get("cpu_temp_celcius")
    if cpu is not None:
        details.append(("CPU temp", f"{cpu:.0f} C"))
    return details


async def _feeder_details_mlat(unit: str) -> list[tuple[str, str]]:
    """Parse recent journald output for mlat-client stats."""
    details: list[tuple[str, str]] = []
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", unit, "--no-pager", "-n", "30", "-o", "cat",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        lines = stdout.decode(errors="replace").splitlines()
    except Exception:
        return details
    import re
    for line in reversed(lines):
        if not details or len(details) < 4:
            m = re.search(r"Results:\s+([\d.]+)\s+positions/minute", line)
            if m and not any(k == "Positions/min" for k, _ in details):
                details.append(("Positions/min", m.group(1)))
            m = re.search(r"Aircraft:\s+(.+)", line)
            if m and not any(k == "Aircraft" for k, _ in details):
                details.append(("Aircraft", m.group(1)))
            m = re.search(r"peer_count:\s+(\d+)", line)
            if m and not any(k == "Peers" for k, _ in details):
                details.append(("Peers", m.group(1)))
            m = re.search(r"Server:\s+(\S+)", line)
            if m and not any(k == "Server" for k, _ in details):
                details.append(("Server", m.group(1)))
    return details


async def _fetch_feeder_details(feeder: dict) -> list[tuple[str, str]]:
    """Dispatch to the appropriate detail fetcher based on status_type."""
    st = feeder.get("status_type")
    try:
        if st == "readsb" and feeder.get("status_path"):
            return _feeder_details_readsb(feeder["status_path"])
        if st == "fr24" and feeder.get("status_url"):
            return await _feeder_details_fr24(feeder["status_url"])
        if st == "piaware" and feeder.get("status_path"):
            return _feeder_details_piaware(feeder["status_path"])
        if st == "mlat":
            return await _feeder_details_mlat(feeder["unit"])
    except Exception:
        pass
    return []


async def _check_single_feeder(feeder: dict) -> dict:
    result = {"name": feeder["name"], "unit": feeder["unit"]}
    coros: list = [_check_systemd_unit(feeder["unit"])]
    if feeder.get("port"):
        coros.append(_check_port(feeder["port"]))
    checks = await asyncio.gather(*coros)
    for check in checks:
        result.update(check)
    systemd_ok = result.get("systemd") == "active"
    port_ok = result.get("port_status", "open") == "open"
    if result.get("systemd") == "unavailable":
        result["overall"] = "unknown"
    elif systemd_ok and port_ok:
        result["overall"] = "ok"
    else:
        result["overall"] = "error"
    result["details"] = await _fetch_feeder_details(feeder)
    return result


async def _check_all_feeders() -> list[dict]:
    return await asyncio.gather(*[_check_single_feeder(f) for f in config.FEEDERS])


@app.get("/feeders", response_class=HTMLResponse)
async def page_feeders(request: Request) -> HTMLResponse:
    feeders = list(await _check_all_feeders()) if config.FEEDERS else []
    return templates.TemplateResponse(request, "feeders.html", {
        "feeders": feeders,
        "has_feeders": bool(config.FEEDERS),
    })
