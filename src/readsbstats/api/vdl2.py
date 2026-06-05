"""VDL2 / ACARS read-only API (opt-in; included only when RSBS_VDL2_ENABLED).

Message data is read from the SEPARATE ``vdl2.db`` via ``vdl2.db.web_conn()``;
the only core ``history.db`` read is `_airline_names` resolving airline names for
the stats card. All handlers are ``def`` (FastAPI runs them in the threadpool;
they only SELECT). Full-text search uses FTS5 when available and falls back to
``LIKE`` otherwise (Pi SQLite-version skew).
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Query

from ..vdl2 import db as vdl2_db
from . import _deps

router = APIRouter()

# Columns returned in list responses — excludes the bulky verbatim ``raw`` JSON.
_LIST_COLS = (
    "id, ts, icao_hex, registration, flight, label, mode, block_id, ack, "
    "msgno, freq, station_id, toaddr, dsta, lat, lon, alt, epu, app_name, "
    "app_ver, body, decoder"
)


def _fts_match(q: str) -> str:
    """Wrap a user query as an FTS5 phrase token so arbitrary punctuation can't
    raise a MATCH syntax error. Doubles embedded quotes per FTS5 escaping."""
    return '"' + q.replace('"', '""') + '"'


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
        where.append("label = ?")
        params.append(label[:64])
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
            else:
                w.append("body LIKE ? ESCAPE '\\'")
                p.append(_like_contains(q))
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


@router.get("/api/vdl2/messages")
def api_vdl2_messages(
    limit: int = Query(100, ge=1, le=100),
    before_id: int | None = Query(None, ge=1),
    label: str | None = Query(None, max_length=64),
    hex: str | None = Query(None, max_length=64),
    reg: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=200),
    since: int | None = Query(None),
    until: int | None = Query(None),
) -> dict:
    """Newest-first message feed with optional filters + full-text search.
    Keyset pagination via ``before_id`` (pass the previous response's
    ``next_before_id`` to load older messages)."""
    return _query_messages(
        limit=limit, before_id=before_id, label=label, hex_=hex, reg=reg,
        since=since, until=until, q=q,
    )


@router.get("/api/vdl2/messages/{icao_hex}")
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


@router.get("/api/vdl2/stats")
def api_vdl2_stats() -> dict:
    """Counts + top labels/airlines + 24h hourly trend for the Stats card."""
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
