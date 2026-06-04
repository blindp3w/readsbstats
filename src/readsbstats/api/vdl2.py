"""VDL2 / ACARS read-only API (opt-in; included only when RSBS_VDL2_ENABLED).

Queries the SEPARATE ``vdl2.db`` via ``vdl2.db.web_conn()`` — never the core
``history.db``. All handlers are ``def`` (FastAPI runs them in the threadpool;
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
) -> dict:
    """All messages from one airframe (by ICAO hex), newest-first."""
    hexv = _deps._parse_icao_path(icao_hex)
    return _query_messages(
        limit=limit, before_id=before_id, label=None, hex_=None, reg=None,
        since=None, until=None, q=q, icao_eq=hexv,
    )


@router.get("/api/vdl2/stats")
def api_vdl2_stats() -> dict:
    """Header counts: total stored, last hour, distinct aircraft."""
    conn = vdl2_db.web_conn()
    now = int(time.time())
    # Single table pass for all three counts (polled every 15 s by the UI;
    # FILTER + COUNT(DISTINCT) keep it one scan instead of three).
    row = conn.execute(
        """
        SELECT COUNT(*)                                              AS total,
               COUNT(*) FILTER (WHERE ts >= ?)                       AS last_hour,
               COUNT(DISTINCT icao_hex)                              AS aircraft
        FROM vdl2_messages
        """,
        (now - 3600,),
    ).fetchone()
    return {"total": row["total"], "last_hour": row["last_hour"], "aircraft": row["aircraft"]}
