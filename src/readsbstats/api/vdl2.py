"""VDL2 / ACARS read-only API (opt-in; included only when RSBS_VDL2_ENABLED).

Message data is read from the SEPARATE ``vdl2.db`` via ``vdl2.db.web_conn()``;
the only core ``history.db`` read is `_airline_names` resolving airline names for
the stats card. All handlers are ``def`` (FastAPI runs them in the threadpool;
they only SELECT). Full-text search uses FTS5 when available and falls back to
``LIKE`` otherwise (Pi SQLite-version skew).
"""
from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import time

from fastapi import APIRouter, HTTPException, Query

from .. import cache, config, schemas
from ..vdl2 import db as vdl2_db
from ..vdl2 import oooi
from ..vdl2 import positions as vdl2_positions
from . import _deps

log = logging.getLogger("vdl2.api")
router = APIRouter()


@contextlib.contextmanager
def _vdl2_guard():
    """Map any VDL2 DB failure (open/attach/query) to a stable 503 so a
    missing/corrupt vdl2.db never surfaces as a 500. (HTTPException — e.g. the
    404 from _parse_icao_path — passes through untouched.)"""
    try:
        yield
    except sqlite3.Error as exc:
        raise HTTPException(503, "VDL2 database unavailable") from exc


def _check_window(since: int | None, until: int | None) -> None:
    if since is not None and until is not None and until <= since:
        raise HTTPException(400, "until must be greater than since")


def vdl2_health() -> dict:
    """VDL2 status block for /api/health (the single source of the runtime
    availability bits the SPA gates on). Never raises — a broken vdl2.db yields
    `available: false` with the rest omitted, not a 500.

    Cached briefly: /api/health is polled by the SPA and the row-`COUNT(*)` is a
    full table scan on a large store, so we don't want it per-poll on the Pi.
    `available` = vdl2.db queryable (drives the Messages tab/Stats); `attach_available`
    = the read-only ATTACH usable (drives the History has_acars filter/badge)."""
    cached = cache._get_cache("vdl2-health")
    if cached is not None:
        return cached
    if not config.VDL2_ENABLED:
        out: dict = {"enabled": False, "available": False}
        cache._set_cache("vdl2-health", out)
        return out
    out = {"enabled": True, "available": False}
    try:
        conn = vdl2_db.web_conn()
        out["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        out["fts"] = vdl2_db.has_fts(conn)
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(ts) AS newest FROM vdl2_messages"
        ).fetchone()
        out["messages"] = row["n"]
        out["newest_ts"] = row["newest"]
        out["newest_age_sec"] = (
            int(time.time()) - row["newest"] if row["newest"] is not None else None
        )
        out["available"] = True
    except sqlite3.Error:
        pass
    try:
        out["attach_available"] = _deps.vdl2_attached(_deps.db())
    except sqlite3.Error:
        out["attach_available"] = False
    cache._set_cache("vdl2-health", out)
    return out

# Columns returned in list responses — excludes the bulky verbatim ``raw`` JSON.
_LIST_COLS = (
    "id, ts, icao_hex, registration, flight, label, mode, block_id, ack, "
    "msgno, freq, station_id, toaddr, dsta, lat, lon, alt, epu, app_name, "
    "app_ver, body, decoder"
)


def _fts_match(q: str) -> str:
    """Build an FTS5 MATCH expr from user input: tokenize, quote each term as a
    phrase (so punctuation can't raise a syntax error), and AND them — so
    'gate EPWA' matches messages containing both terms, not only the exact
    adjacent phrase. Capped at 8 terms."""
    terms = re.findall(r"[\w.-]+", q, flags=re.UNICODE)
    if not terms:
        return '""'
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in terms[:8])


def _like_prefix(value: str) -> str:
    """Escape LIKE wildcards in user input, then append ``%`` for a prefix match.
    Pair with ``ESCAPE '\\'`` in the SQL."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _like_contains(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def _rows_to_messages(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _query_messages(
    *, limit: int, before_id: int | None, label: str | None,
    hex_: str | None, reg: str | None, since: int | None, until: int | None,
    q: str | None, icao_eq: str | None = None,
) -> dict:
    conn = vdl2_db.web_conn()
    where: list[str] = []
    params: list = []
    if icao_eq is not None:
        where.append("icao_hex = ?")
        params.append(icao_eq)
    if before_id is not None:
        where.append("id < ?")
        params.append(before_id)
    if label:
        # Labels are uppercased at ingest (normalize._label), so match case-insensitively.
        where.append("label = ?")
        params.append(label[:64].upper())
    if hex_:
        where.append("icao_hex LIKE ? ESCAPE '\\'")
        params.append(_like_prefix(hex_.lower()[:64]))
    if reg:
        where.append("registration LIKE ? ESCAPE '\\' COLLATE NOCASE")
        params.append(_like_prefix(reg[:64]))
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if until is not None:
        where.append("ts < ?")
        params.append(until)

    def _run(use_fts: bool):
        w, p = list(where), list(params)
        if q:
            if use_fts:
                w.append("id IN (SELECT rowid FROM vdl2_fts WHERE vdl2_fts MATCH ?)")
                p.append(_fts_match(q))
            elif len(q.strip()) >= 2:
                # LIKE '%x%' on a 1-char term is a useless full scan — skip it.
                w.append("body LIKE ? ESCAPE '\\'")
                p.append(_like_contains(q.strip()))
        sql = f"SELECT {_LIST_COLS} FROM vdl2_messages"
        if w:
            sql += " WHERE " + " AND ".join(w)
        sql += " ORDER BY id DESC LIMIT ?"
        p.append(limit)
        return conn.execute(sql, p).fetchall()

    # Prefer FTS5 when the index exists, but never 500 if the running SQLite
    # build can't actually run MATCH (build/DB version skew) — fall back to LIKE.
    try:
        rows = _run(use_fts=bool(q) and vdl2_db.has_fts(conn))
    except sqlite3.OperationalError:
        rows = _run(use_fts=False)
    messages = _rows_to_messages(rows)
    next_before_id = messages[-1]["id"] if len(messages) == limit else None
    return {"messages": messages, "next_before_id": next_before_id}


@router.get("/api/vdl2/messages", response_model=schemas.Vdl2MessagesResponse,
            response_model_exclude_unset=True)
def api_vdl2_messages(
    limit: int = Query(100, ge=1, le=100),
    before_id: int | None = Query(None, ge=1),
    label: str | None = Query(None, max_length=64),
    hex: str | None = Query(None, max_length=64),
    reg: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=200),
    since: int | None = Query(None, ge=0),
    until: int | None = Query(None, ge=0),
) -> dict:
    """Newest-first message feed (by ingest id) with optional filters + full-text
    search. Keyset pagination via ``before_id`` (pass the previous response's
    ``next_before_id`` to load older messages)."""
    _check_window(since, until)
    with _vdl2_guard():
        return _query_messages(
            limit=limit, before_id=before_id, label=label, hex_=hex, reg=reg,
            since=since, until=until, q=q,
        )


@router.get("/api/vdl2/messages/{icao_hex}", response_model=schemas.Vdl2MessagesResponse,
            response_model_exclude_unset=True)
def api_vdl2_aircraft(
    icao_hex: str,
    limit: int = Query(100, ge=1, le=100),
    before_id: int | None = Query(None, ge=1),
    q: str | None = Query(None, max_length=200),
    since: int | None = Query(None, ge=0),
    until: int | None = Query(None, ge=0),
) -> dict:
    """All messages from one airframe (by ICAO hex), newest-first. ``since``/
    ``until`` (epoch) scope it to a flight window for the flight-detail panel."""
    hexv = _deps._parse_icao_path(icao_hex)
    _check_window(since, until)
    with _vdl2_guard():
        return _query_messages(
            limit=limit, before_id=before_id, label=None, hex_=None, reg=None,
            since=since, until=until, q=q, icao_eq=hexv,
        )


def _airline_names(codes: list[str]) -> dict[str, str]:
    """Resolve 2-char IATA airline codes → names via the core history.db
    `airlines` table (airlines live there, not in vdl2.db). ACARS `flight` ids
    are IATA-format (e.g. 'LO6550'), so we key on `iata_code`, not `icao_code`.
    Degrades to {} on any error so the stats card still shows codes."""
    codes = [c for c in codes if c]
    if not codes:
        return {}
    try:
        core = _deps.db()
        ph = ",".join("?" * len(codes))
        rows = core.execute(
            f"SELECT iata_code, name FROM airlines WHERE iata_code IN ({ph})", codes
        ).fetchall()
        return {r["iata_code"]: r["name"] for r in rows if r["iata_code"] and r["name"]}
    except sqlite3.Error:
        return {}


@router.get("/api/vdl2/stats", response_model=schemas.Vdl2StatsResponse,
            response_model_exclude_unset=True)
def api_vdl2_stats() -> dict:
    """Counts + top labels/airlines + 24h hourly trend for the Stats card.
    Cached (~30 s) since the page polls it and the aggregates scan the table."""
    cached = cache._get_cache("vdl2-stats")
    if cached is not None:
        return cached
    with _vdl2_guard():
        t0 = time.perf_counter()
        result = _compute_stats()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > 250:
            log.warning("vdl2 stats query slow: %.0f ms", elapsed_ms)
        cache._set_cache("vdl2-stats", result)
        return result


def _compute_stats() -> dict:
    conn = vdl2_db.web_conn()
    now = int(time.time())
    # Single table pass for the three headline counts.
    row = conn.execute(
        """
        SELECT COUNT(*)                        AS total,
               COUNT(*) FILTER (WHERE ts >= ?) AS last_hour,
               COUNT(DISTINCT icao_hex)        AS aircraft
        FROM vdl2_messages
        """,
        (now - 3600,),
    ).fetchone()

    top_labels = [
        {"label": r["label"], "messages": r["messages"], "aircraft": r["aircraft"]}
        for r in conn.execute(
            "SELECT label, COUNT(*) AS messages, COUNT(DISTINCT icao_hex) AS aircraft "
            "FROM vdl2_messages WHERE label IS NOT NULL "
            "GROUP BY label ORDER BY messages DESC LIMIT 10"
        ).fetchall()
    ]

    # ACARS flight ids are IATA-format (e.g. 'LO6550'); group on the 2-char
    # operator prefix (NOT substr(,1,3), which would split 'LO6550' and 'LO0304'
    # into different keys) and resolve names via airlines.iata_code.
    airline_rows = conn.execute(
        "SELECT substr(flight, 1, 2) AS code, COUNT(*) AS messages "
        "FROM vdl2_messages WHERE flight IS NOT NULL AND length(flight) >= 3 "
        "GROUP BY code ORDER BY messages DESC LIMIT 10"
    ).fetchall()
    names = _airline_names([r["code"] for r in airline_rows])
    top_airlines = [
        {"code": r["code"], "messages": r["messages"], "name": names.get(r["code"])}
        for r in airline_rows
    ]

    # Last-24h message rate, zero-filled to 24 hourly buckets oldest→newest.
    hourly = _rate_buckets(conn, now, 3600, 24)

    return {
        "total": row["total"],
        "last_hour": row["last_hour"],
        "aircraft": row["aircraft"],
        "top_labels": top_labels,
        "top_airlines": top_airlines,
        "hourly": hourly,
        "flights_overlap_pct": _flights_overlap_pct(),
    }


def _rate_buckets(conn, now: int, unit_sec: int, n: int) -> list[int]:
    """Message count per `unit_sec`-wide bucket for the last `n` buckets,
    zero-filled oldest→newest (index `n-1` is the current partial bucket).

    Keys off ``(now - ts)`` so the current partial bucket lands in the newest
    slot rather than spilling into an (n+1)th bucket that callers would drop.
    Shared by the Stats 24h hourly trend and the reception 60-min sparkline."""
    since = now - unit_sec * n
    counts = {
        r["ago"]: r["c"]
        for r in conn.execute(
            "SELECT CAST((? - ts) / ? AS INT) AS ago, COUNT(*) AS c "
            "FROM vdl2_messages WHERE ts >= ? GROUP BY ago",
            (now, unit_sec, since),
        ).fetchall()
    }
    return [counts.get(n - 1 - i, 0) for i in range(n)]


def _flights_overlap_pct() -> float | None:
    """% of flights in the last 24h whose airframe also transmitted ACARS during
    the flight's window. Runs on the CORE history.db connection with vdl2.db
    ATTACHed read-only (the rest of stats reads the vdl2.db connection). Returns
    None when the ATTACH is unavailable or on any error — must never fail the
    stats card, and must never turn the cached stats payload into a 503.

    Heaviest query in the VDL2 surface (flights scan + correlated EXISTS); it
    rides inside the ~30 s stats cache. The 24h window filters on **first_seen**
    (index-served by idx_flights_first) rather than last_seen (which has no
    index → a full flights scan); the per-row EXISTS is served by idx_vdl2_icao."""
    try:
        core = _deps.db()
        # Re-attempt the attach per request (idempotent) so a vdl2.db created
        # after this thread's core connection opened still attaches.
        _deps._maybe_attach_vdl2(core)
        if not _deps.vdl2_attached(core):
            return None
        since = int(time.time()) - 24 * 3600
        row = core.execute(
            """
            SELECT COUNT(*) AS flights,
                   SUM(CASE WHEN EXISTS(
                       SELECT 1 FROM vdl2db.vdl2_messages v
                       WHERE v.icao_hex = f.icao_hex
                         AND v.ts >= f.first_seen AND v.ts <= f.last_seen
                   ) THEN 1 ELSE 0 END) AS with_acars
            FROM flights f
            WHERE f.first_seen >= ?
            """,
            (since,),
        ).fetchone()
    except sqlite3.Error:
        return None
    flights = row["flights"] or 0
    if not flights:
        return None
    return round(100.0 * (row["with_acars"] or 0) / flights, 1)


@router.get("/api/vdl2/reception", response_model=schemas.Vdl2ReceptionResponse,
            response_model_exclude_unset=True)
def api_vdl2_reception() -> dict:
    """Receiver-health card for the Metrics page: message rate, per-frequency
    activity, distinct aircraft, and feed freshness. vdlm2dec-only (no signal
    level). Cached (~30 s) — polled by the SPA and the aggregates scan the table."""
    cached = cache._get_cache("vdl2-reception")
    if cached is not None:
        return cached
    with _vdl2_guard():
        t0 = time.perf_counter()
        result = _compute_reception()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > 250:
            log.warning("vdl2 reception query slow: %.0f ms", elapsed_ms)
        cache._set_cache("vdl2-reception", result)
        return result


def _compute_reception() -> dict:
    conn = vdl2_db.web_conn()
    now = int(time.time())
    # Counts are bounded to the last 24h (index-served on ts). Freshness is a
    # SEPARATE, table-wide MAX(ts) — a feed stale for >24h must still report its
    # real newest age, not None. MAX(ts) is served by idx_vdl2_ts, so it's O(1).
    row = conn.execute(
        """
        SELECT COUNT(*)                                AS msgs_24h,
               COUNT(*) FILTER (WHERE ts >= ?)         AS last_hour,
               COUNT(*) FILTER (WHERE ts >= ?)         AS last_min,
               COUNT(DISTINCT CASE WHEN ts >= ? THEN icao_hex END) AS ac_hour
        FROM vdl2_messages
        WHERE ts >= ?
        """,
        (now - 3600, now - 60, now - 3600, now - 24 * 3600),
    ).fetchone()
    newest = conn.execute("SELECT MAX(ts) AS newest FROM vdl2_messages").fetchone()["newest"]

    # Per-frequency activity over the last 24h. Group on ROUND(freq, 3): `freq`
    # is a REAL in MHz, so grouping on the raw float would fragment the four VHF
    # channels (136.725/.775/.875/.975) on representation drift.
    per_freq = [
        {"freq_mhz": r["freq_mhz"], "messages": r["messages"], "aircraft": r["aircraft"]}
        for r in conn.execute(
            "SELECT ROUND(freq, 3) AS freq_mhz, COUNT(*) AS messages, "
            "COUNT(DISTINCT icao_hex) AS aircraft "
            "FROM vdl2_messages WHERE freq IS NOT NULL AND ts >= ? "
            "GROUP BY freq_mhz ORDER BY messages DESC",
            (now - 24 * 3600,),
        ).fetchall()
    ]

    # Messages/min over the last 60 minutes, zero-filled oldest→newest (index 59
    # is the current minute).
    rate_sparkline = _rate_buckets(conn, now, 60, 60)

    return {
        "msgs_last_min": row["last_min"],
        "msgs_last_hour": row["last_hour"],
        "msgs_24h": row["msgs_24h"],
        "aircraft_last_hour": row["ac_hour"],
        "newest_ts": newest,
        "newest_age_sec": (now - newest) if newest is not None else None,
        "per_freq": per_freq,
        "rate_sparkline": rate_sparkline,
    }


@router.get("/api/vdl2/oooi/{icao_hex}", response_model=schemas.Vdl2OooiSummary,
            response_model_exclude_unset=True)
def api_vdl2_oooi(
    icao_hex: str,
    since: int | None = Query(None, ge=0),
    until: int | None = Query(None, ge=0),
) -> dict:
    """OOOI block-time summary for one airframe over a flight window. Scans the
    airframe's ACARS bodies (OOOI is in the free-text body, NOT the label) and
    returns the latest DEP + latest ARR, plus a `dsta` destination fallback.
    EXPERIMENTAL — commonly empty on an H1-dominated feed; the SPA hides the card
    when `has_oooi` is false and no `dsta`."""
    hexv = _deps._parse_icao_path(icao_hex)
    _check_window(since, until)
    with _vdl2_guard():
        return _compute_oooi(hexv, since, until)


# Bound the OOOI body scan per flight window — generous, but caps a pathological
# airframe with tens of thousands of messages in one window.
_OOOI_SCAN_LIMIT = 1000


def _compute_oooi(icao_hex: str, since: int | None, until: int | None) -> dict:
    conn = vdl2_db.web_conn()
    where = ["icao_hex = ?"]
    params: list = [icao_hex]
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if until is not None:
        where.append("ts < ?")
        params.append(until)
    params.append(_OOOI_SCAN_LIMIT)
    rows = conn.execute(
        f"SELECT body, dsta, ts FROM vdl2_messages WHERE {' AND '.join(where)} "
        "ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()

    dep = arr = None
    dsta = None
    # Rows are newest-first, so the first DEP/ARR we parse is the most recent.
    for r in rows:
        if dsta is None and r["dsta"]:
            dsta = r["dsta"]
        parsed = oooi.parse_oooi(r["body"])
        if parsed is None:
            continue
        if parsed["type"] == "DEP" and dep is None:
            dep = {**parsed, "ts": r["ts"]}
        elif parsed["type"] == "ARR" and arr is None:
            arr = {**parsed, "ts": r["ts"]}
        if dep is not None and arr is not None and dsta is not None:
            break
    return {"dep": dep, "arr": arr, "dsta": dsta, "has_oooi": dep is not None or arr is not None}


# Map-overlay caps. Positions are sampled to avoid shipping a huge GeoJSON to the
# map when a chatty position-reporting fleet is in range.
_POSITIONS_CAP = 2000


@router.get("/api/vdl2/active", response_model=schemas.Vdl2ActiveResponse,
            response_model_exclude_unset=True)
def api_vdl2_active(minutes: int = Query(10, ge=1, le=120)) -> dict:
    """ICAO hexes that transmitted ACARS in the last `minutes` minutes. Drives
    the map's optional 'transmitting ACARS now' marker badge — fetched only when
    the overlay toggle is on; the live snapshot path is never touched. Cached ~30 s."""
    cached = cache._get_cache(f"vdl2-active-{minutes}")
    if cached is not None:
        return cached
    with _vdl2_guard():
        conn = vdl2_db.web_conn()
        cutoff = int(time.time()) - minutes * 60
        rows = conn.execute(
            "SELECT DISTINCT icao_hex FROM vdl2_messages "
            "WHERE ts >= ? AND icao_hex IS NOT NULL",
            (cutoff,),
        ).fetchall()
        hexes = [r["icao_hex"] for r in rows]
        out = {"icao_hex": hexes, "count": len(hexes)}
        cache._set_cache(f"vdl2-active-{minutes}", out)
        return out


@router.get("/api/vdl2/positions", response_model=schemas.Vdl2PositionsResponse,
            response_model_exclude_unset=True)
def api_vdl2_positions(minutes: int = Query(60, ge=1, le=1440)) -> dict:
    """VDL2-derived positions from the last `minutes` minutes, for the optional
    map overlay. Two sources, precise preferred (validated against a real feed):
    Label-16 AUTPOS bodies carry **precise** (~0.001°) coordinates parsed here;
    the lat/lon columns hold only **coarse** (~0.1°) VDL2 XID link-frame fixes
    used as a fallback. Each point carries `precise`. Sparse on an H1-dominated
    feed. Capped + cached ~30 s."""
    cached = cache._get_cache(f"vdl2-positions-{minutes}")
    if cached is not None:
        return cached
    with _vdl2_guard():
        out = _compute_positions(minutes)
        cache._set_cache(f"vdl2-positions-{minutes}", out)
        return out


def _compute_positions(minutes: int) -> dict:
    """Two index-served candidate queries merged in Python (a single OR query
    full-scans; see idx_vdl2_label_ts_id / idx_vdl2_pos_ts_id). Precise fixes are
    parsed ONLY from Label-16 AUTPOS bodies (so a coordinate-looking body on a
    non-16 row can't masquerade as precise); coarse XID column fixes are the
    fallback. Independent caps + a final merge/cap so a burst of no-fix Label-16
    rows can't starve valid coarse points."""
    conn = vdl2_db.web_conn()
    cutoff = int(time.time()) - minutes * 60

    label_rows = conn.execute(
        "SELECT id, icao_hex, ts, label, body FROM vdl2_messages "
        "WHERE label = '16' AND ts >= ? ORDER BY ts DESC, id DESC LIMIT ?",
        (cutoff, _POSITIONS_CAP),
    ).fetchall()
    coarse_rows = conn.execute(
        "SELECT id, lat, lon, icao_hex, ts, label FROM vdl2_messages "
        "WHERE lat IS NOT NULL AND lon IS NOT NULL AND ts >= ? "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (cutoff, _POSITIONS_CAP),
    ).fetchall()

    points: list[dict] = []
    seen: set[int] = set()
    for r in label_rows:
        parsed = vdl2_positions.parse_position(r["body"])
        if parsed is None:
            continue   # no-fix Label-16 body — discarded (does not consume the final cap)
        seen.add(r["id"])
        points.append({
            "_id": r["id"], "lat": parsed["lat"], "lon": parsed["lon"],
            "icao_hex": r["icao_hex"], "ts": r["ts"], "label": r["label"], "precise": True,
        })
    for r in coarse_rows:
        if r["id"] in seen:
            continue   # same row already emitted as a precise fix
        points.append({
            "_id": r["id"], "lat": r["lat"], "lon": r["lon"],
            "icao_hex": r["icao_hex"], "ts": r["ts"], "label": r["label"], "precise": False,
        })

    points.sort(key=lambda p: (p["ts"] or 0, p["_id"]), reverse=True)
    points = points[:_POSITIONS_CAP]
    for p in points:
        del p["_id"]   # internal merge key — not part of the public Vdl2Position contract
    return {"points": points, "count": len(points)}
