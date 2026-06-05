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
    # `ago` = whole hours before now (0 = current partial hour); index 23 is the
    # newest. Bucketing by (ts-since) would push the current hour into a 25th
    # bucket and drop it — the most relevant data — so key off (now - ts).
    since = now - 24 * 3600
    counts = {
        r["ago"]: r["c"]
        for r in conn.execute(
            "SELECT CAST((? - ts) / 3600 AS INT) AS ago, COUNT(*) AS c "
            "FROM vdl2_messages WHERE ts >= ? GROUP BY ago",
            (now, since),
        ).fetchall()
    }
    hourly = [counts.get(23 - i, 0) for i in range(24)]

    return {
        "total": row["total"],
        "last_hour": row["last_hour"],
        "aircraft": row["aircraft"],
        "top_labels": top_labels,
        "top_airlines": top_airlines,
        "hourly": hourly,
    }
