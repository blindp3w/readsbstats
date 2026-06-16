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
from ..vdl2 import m1bpos
from ..vdl2 import oooi
from ..vdl2 import positions as vdl2_positions
from ..vdl2 import rte
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
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        body = d.get("body")
        if body:
            # Filed route from the two body shapes that carry one: #M1BPOS /RP:
            # blocks and Teledyne RTE messages. Only set when parseable ->
            # exclude_unset omits it elsewhere.
            route = None
            if body.startswith("#M1BPOS"):
                route = m1bpos.parse_route(body)
            elif body.startswith("RTE ") or body.startswith("#T1BRTE"):
                route = rte.parse_route(body)
            if route is not None:
                d["filed_route"] = route
        out.append(d)
    return out


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
            else:
                # BUG-4/F09: no FTS and the term is too short to LIKE. The user
                # DID supply a search term, so a too-short term must match
                # nothing — never silently fall through to the unfiltered
                # newest-N feed. A guaranteed-false predicate yields [].
                w.append("0")
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



@router.get("/api/vdl2/oooi/{icao_hex}", response_model=schemas.Vdl2OooiSummary,
            response_model_exclude_unset=True)
def api_vdl2_oooi(
    icao_hex: str,
    since: int | None = Query(None, ge=0),
    until: int | None = Query(None, ge=0),
) -> dict:
    """OOOI block-time summary for one airframe over a flight window. The latest
    slash-TEI DEP/ARR bodies win; where those are absent (the norm on air-side
    feeds) events are synthesized from Q-series QP/QQ/QR/QS compact reports, and
    airline-defined label-49 movement reports fill route gaps plus the `dsta`
    destination fallback. The SPA hides the card when `has_oooi` is false and no
    `dsta`."""
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
        f"SELECT body, dsta, ts, label, registration, flight "
        f"FROM vdl2_messages WHERE {' AND '.join(where)} "
        "ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()

    dep = arr = None
    dsta = None
    q_partials: list[dict] = []
    route49: dict | None = None
    # Rows are newest-first, so the first DEP/ARR we parse is the most recent.
    for r in rows:
        if dsta is None and r["dsta"]:
            dsta = r["dsta"]
        lab = r["label"]
        if lab in oooi.Q_PHASES:
            q = oooi.parse_qseries(lab, r["body"])
            if q is not None:
                q_partials.append({**q, "ts": r["ts"],
                                   "registration": r["registration"],
                                   "flight": r["flight"]})
        elif lab == "49" and route49 is None:
            route49 = oooi.parse_label49(r["body"])
        parsed = oooi.parse_oooi(r["body"])
        if parsed is not None:
            if parsed["type"] == "DEP" and dep is None:
                dep = {**parsed, "ts": r["ts"]}
            elif parsed["type"] == "ARR" and arr is None:
                arr = {**parsed, "ts": r["ts"]}
        # Early exit once slash-TEI gave a complete answer. Accepted trade-off:
        # this can skip a label-49 route fill for a TEI event missing DA/DS —
        # rare, and bounded work wins.
        if dep is not None and arr is not None and dsta is not None:
            break

    # Synthesize events from Q-series partials where slash-TEI gave nothing.
    if q_partials and (dep is None or arr is None):
        phases = _dominant_q_phases(q_partials)
        if dep is None:
            dep = _synth_q_event("DEP", phases.get("out"), phases.get("off"))
        if arr is None:
            arr = _synth_q_event("ARR", phases.get("on"), phases.get("in"))

    # Airline-defined label-49 movement reports carry a dep+arr pair: fill route
    # gaps on events, and serve as the `dsta` fallback when nothing else parsed.
    if route49 is not None:
        for ev in (dep, arr):
            if ev is not None:
                if ev["dep_icao"] is None:
                    ev["dep_icao"] = route49["dep_icao"]
                if ev["dest_icao"] is None:
                    ev["dest_icao"] = route49["dest_icao"]
        if dsta is None:
            dsta = route49["dest_icao"]

    return {"dep": dep, "arr": arr, "dsta": dsta, "has_oooi": dep is not None or arr is not None}


def _dominant_q_phases(partials: list[dict]) -> dict[str, dict]:
    """Group Q-series partials by city pair and return ``{phase: partial}`` for
    the dominant pair — most distinct phases, ties to the pair with the newest
    partial. A quick turnaround's next-leg OUT report lands inside the flight
    window's slack; without this, its `t_out` would be mixed into this leg's
    synthesized DEP and the card would show a false route mismatch."""
    by_pair: dict[tuple, dict] = {}
    for p in partials:   # newest-first: first hit per pair/phase is the newest
        pair = (p["dep_icao"], p["dest_icao"])
        slot = by_pair.setdefault(pair, {"phases": {}, "newest_ts": p["ts"]})
        slot["phases"].setdefault(p["phase"], p)
    best: dict[str, dict] = {}
    best_key = None
    for slot in by_pair.values():
        key = (len(slot["phases"]), slot["newest_ts"])
        if best_key is None or key > best_key:
            best_key, best = key, slot["phases"]
    return best


def _synth_q_event(ev_type: str, first: dict | None, second: dict | None) -> dict | None:
    """Build a Vdl2OooiEvent-shaped dict from up to two Q-series phase partials
    (DEP: out+off, ARR: on+in), or ``None`` when both are absent. Emits exactly
    the event-contract keys — `schemas.ApiModel` is ``extra="allow"``, so any
    internal key (phase/t/t2) would leak into the JSON response."""
    contributors = [p for p in (first, second) if p is not None]
    if not contributors:
        return None
    newest = max(contributors, key=lambda p: p["ts"])
    t_out = t_off = t_on = t_in = None
    if ev_type == "DEP":
        if first is not None:
            t_out = first["t"]
        if second is not None:
            t_off = second["t"]
            if t_out is None:
                t_out = second.get("t2")   # QQ (OFF) echoes the OUT time
    else:
        if first is not None:
            t_on = first["t"]
        if second is not None:
            t_in = second["t"]
    return {
        "type": ev_type,
        "registration": newest["registration"],
        "flight": newest["flight"],
        "dep_icao": newest["dep_icao"],
        "dest_icao": newest["dest_icao"],
        "t_out": t_out,
        "t_off": t_off,
        "t_on": t_on,
        "t_in": t_in,
        "ts": newest["ts"],
    }


_TIMESERIES_TOP_FREQS = 6


def _timeseries_bucket(span: int) -> int:
    """Bucket width (seconds) for a window span. Mirrors /api/metrics, but with a
    60 s minimum — vdl2_messages are individual rows, so there is no raw mode."""
    if span <= 86_400:
        return 60
    if span <= 604_800:
        return 300
    if span <= 2_592_000:
        return 900
    if span <= 7_776_000:
        return 3600
    return 14400


def _fmt_freq(f: float) -> str:
    return f"{f:g}"


# Largest window the timeseries endpoint will aggregate. The Metrics range picker
# only offers up to 90 d; cap a little above a year so a crafted/buggy request
# (e.g. from=0) can't allocate a multi-million-entry bucket grid on the Pi.
_TIMESERIES_MAX_SPAN = 366 * 86_400


@router.get("/api/vdl2/timeseries", response_model=schemas.Vdl2TimeseriesResponse,
            response_model_exclude_unset=True)
def api_vdl2_timeseries(
    from_ts: int | None = Query(None, alias="from", ge=0, le=10_000_000_000),
    to_ts: int | None = Query(None, alias="to", ge=0, le=10_000_000_000),
) -> dict:
    """Bucketed reception time-series (msgs/min total + per top-frequency) for the
    Metrics page's two VDL2 charts, over the picker's [from, to] window. Columnar
    like /api/metrics so the frontend reuses its chart builders. Not cached — from/to
    are stable per page mount and the SPA holds a 30 s staleTime. The window is
    capped (`_TIMESERIES_MAX_SPAN`) so an over-wide request can't blow up memory."""
    now = int(time.time())
    if to_ts is None:
        to_ts = now
    if from_ts is None:
        from_ts = to_ts - 86_400
    if to_ts <= from_ts:
        raise HTTPException(400, "to must be greater than from")
    if to_ts - from_ts > _TIMESERIES_MAX_SPAN:
        raise HTTPException(400, "window too large")
    with _vdl2_guard():
        t0 = time.perf_counter()
        result = _compute_timeseries(from_ts, to_ts)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > 250:
            log.warning("vdl2 timeseries query slow: %.0f ms", elapsed_ms)
        return result


def _bucket_fill(rows, n: int) -> list[int]:
    """Scatter ``(bi, c)`` rows into a zero-filled list of length ``n``.

    ``rows`` is any iterable of mappings with a ``bi`` bucket index and a ``c``
    count. Out-of-range indices are ignored; buckets with no row stay 0.
    """
    out = [0] * n
    for r in rows:
        i = r["bi"]
        if 0 <= i < n:
            out[i] = r["c"]
    return out


def _compute_timeseries(from_ts: int, to_ts: int) -> dict:
    conn = vdl2_db.web_conn()
    bucket = _timeseries_bucket(to_ts - from_ts)

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM vdl2_messages WHERE ts >= ? AND ts < ?",
        (from_ts, to_ts),
    ).fetchone()["n"]
    newest = conn.execute("SELECT MAX(ts) AS newest FROM vdl2_messages").fetchone()["newest"]

    top = [
        r["f"] for r in conn.execute(
            "SELECT ROUND(freq, 3) AS f, COUNT(*) AS c FROM vdl2_messages "
            "WHERE freq IS NOT NULL AND ts >= ? AND ts < ? "
            "GROUP BY f ORDER BY c DESC, f LIMIT ?",
            (from_ts, to_ts, _TIMESERIES_TOP_FREQS),
        ).fetchall()
    ]

    n = max(1, (to_ts - from_ts + bucket - 1) // bucket)
    buckets = [from_ts + i * bucket for i in range(n)]
    n = len(buckets)

    rate = _bucket_fill(
        conn.execute(
            "SELECT CAST((ts - ?) / ? AS INT) AS bi, COUNT(*) AS c FROM vdl2_messages "
            "WHERE ts >= ? AND ts < ? GROUP BY bi",
            (from_ts, bucket, from_ts, to_ts),
        ).fetchall(),
        n,
    )

    cols: dict = {}
    if top:
        freq_rows = conn.execute(
            "SELECT CAST((ts - ?) / ? AS INT) AS bi, ROUND(freq, 3) AS f, COUNT(*) AS c "
            "FROM vdl2_messages "
            "WHERE freq IS NOT NULL AND ts >= ? AND ts < ? GROUP BY bi, f",
            (from_ts, bucket, from_ts, to_ts),
        ).fetchall()
        # One zero-filled column per top frequency (a freq with no rows in the
        # window still gets an all-zero column).
        cols = {f: _bucket_fill([r for r in freq_rows if r["f"] == f], n) for f in top}

    per_min = 60.0 / bucket

    def norm(counts: list[int]) -> list[float]:
        return [round(c * per_min, 2) for c in counts]

    data = [[float(b) for b in buckets], norm(rate)] + [norm(cols[f]) for f in top]
    return {
        "bucket_seconds": bucket,
        "metrics": ["rate"] + [_fmt_freq(f) for f in top],
        "freqs": top,
        "total": total,
        "newest_ts": newest,
        "newest_age_sec": (int(time.time()) - newest) if newest is not None else None,
        "data": data,
    }


# Map-overlay caps. Positions are sampled to avoid shipping a huge GeoJSON to the
# map when a chatty position-reporting fleet is in range.
_POSITIONS_CAP = 2000
# Label-16 candidates are over-fetched (then parsed + discarded) so a burst of
# newer no-fix AUTPOS rows can't crowd parseable older precise fixes out of the
# scan before the final cap. Bounded so the scan still terminates.
_POSITIONS_LABEL16_SCAN_CAP = 8000
# #M1BPOS bodies are a small fraction of the H1-heavy feed; over-fetch within the
# window (then parse + discard non-position rows) so the final cap isn't consumed
# by no-fix rows. Bounded so the scan still terminates.
_POSITIONS_M1BPOS_SCAN_CAP = 8000
# LOT `59,G` ground-telemetry positions (label 36); same over-fetch+parse+discard
# rationale (the label-37 status sub-form shares the prefix and is parsed out).
_POSITIONS_59G_SCAN_CAP = 8000


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
    map overlay. Three sources, precise preferred (validated against a real feed):
    Label-16 AUTPOS bodies **and** `#M1BPOS` (Honeywell FMS) bodies both carry
    **precise** (~0.001°) coordinates parsed here; the lat/lon columns hold only
    **coarse** (~0.1°) VDL2 XID link-frame fixes used as a fallback. Each point
    carries `precise`. Sparse on an H1-dominated feed. Capped + cached ~30 s."""
    cached = cache._get_cache(f"vdl2-positions-{minutes}")
    if cached is not None:
        return cached
    with _vdl2_guard():
        out = _compute_positions(minutes)
        cache._set_cache(f"vdl2-positions-{minutes}", out)
        return out


def _compute_positions(minutes: int) -> dict:
    """Four candidate queries merged in Python (a single OR query full-scans). The
    label-16 and coarse queries are index-served (idx_vdl2_label_ts_id /
    idx_vdl2_pos_ts_id); the `#M1BPOS` and `59,G` queries are time-window-bounded row
    scans (idx_vdl2_ts bounds `ts >=`, then a `body LIKE` filter), capped so they still
    terminate. Precise fixes are parsed from Label-16 AUTPOS, `#M1BPOS`, and LOT `59,G`
    bodies (so a coordinate-looking body on any other row can't masquerade as precise);
    coarse XID column fixes are the fallback. Independent caps + over-fetched scans + a
    final merge/cap so a burst of no-fix rows can't starve valid coarse OR older precise
    points."""
    conn = vdl2_db.web_conn()
    cutoff = int(time.time()) - minutes * 60

    label_rows = conn.execute(
        "SELECT id, icao_hex, ts, label, body FROM vdl2_messages "
        "WHERE label = '16' AND ts >= ? ORDER BY ts DESC, id DESC LIMIT ?",
        (cutoff, _POSITIONS_LABEL16_SCAN_CAP),
    ).fetchall()
    coarse_rows = conn.execute(
        "SELECT id, lat, lon, icao_hex, ts, label FROM vdl2_messages "
        "WHERE lat IS NOT NULL AND lon IS NOT NULL AND ts >= ? "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (cutoff, _POSITIONS_CAP),
    ).fetchall()
    m1bpos_rows = conn.execute(
        "SELECT id, icao_hex, ts, label, body FROM vdl2_messages "
        "WHERE ts >= ? AND body LIKE '#M1BPOS%' ORDER BY ts DESC, id DESC LIMIT ?",
        (cutoff, _POSITIONS_M1BPOS_SCAN_CAP),
    ).fetchall()
    g59_rows = conn.execute(
        "SELECT id, icao_hex, ts, label, body FROM vdl2_messages "
        "WHERE ts >= ? AND body LIKE '59,G,%' ORDER BY ts DESC, id DESC LIMIT ?",
        (cutoff, _POSITIONS_59G_SCAN_CAP),
    ).fetchall()

    def _point(r, lat, lon, precise) -> dict:
        # `_id` is an internal merge/sort key, stripped before the response.
        return {
            "_id": r["id"], "lat": lat, "lon": lon, "icao_hex": r["icao_hex"],
            "ts": r["ts"], "label": r["label"], "precise": precise,
        }

    points: list[dict] = []
    seen: set[int] = set()
    for r in label_rows:
        parsed = vdl2_positions.parse_position(r["body"])
        if parsed is None:
            continue   # no-fix Label-16 body — discarded (does not consume the final cap)
        seen.add(r["id"])
        points.append(_point(r, parsed["lat"], parsed["lon"], True))
    for r in m1bpos_rows:
        parsed = m1bpos.parse_position(r["body"])
        if parsed is None:
            continue   # non-position #M1BPOS body — discarded (does not consume the final cap)
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        points.append(_point(r, parsed["lat"], parsed["lon"], True))
    for r in g59_rows:
        parsed = vdl2_positions.parse_59g(r["body"])
        if parsed is None:
            continue   # non-position 59,G (label-37 status) — discarded, no cap consumed
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        points.append(_point(r, parsed["lat"], parsed["lon"], True))
    for r in coarse_rows:
        if r["id"] in seen:
            continue   # same row already emitted as a precise fix
        points.append(_point(r, r["lat"], r["lon"], False))

    points.sort(key=lambda p: (p["ts"] or 0, p["_id"]), reverse=True)
    points = points[:_POSITIONS_CAP]
    for p in points:
        del p["_id"]   # internal merge key — not part of the public Vdl2Position contract
    return {"points": points, "count": len(points)}
