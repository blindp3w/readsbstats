"""Service liveness, receiver metrics time-series, and receiver-health rule report."""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .. import cache, health as receiver_health
from . import _deps


router = APIRouter()


@router.get("/api/metrics")
def api_metrics(
    from_ts: int | None = Query(None, alias="from"),
    to_ts:   int | None = Query(None, alias="to"),
    metrics: str = "signal,noise",
) -> dict:
    """
    Return receiver metrics as columnar arrays (uPlot-native format).

    Query params:
        from   — start epoch (default: 24 h ago)
        to     — end epoch (default: now)
        metrics — comma-separated column names from _METRICS_COLS

    Non-integer `from` / `to` are rejected at the FastAPI layer with HTTP 422
    rather than the 500 the old `int(request.query_params.get(...))` path
    produced.  See improvements.md #115.
    """
    now = int(time.time())
    if from_ts is None:
        from_ts = now - 86400
    if to_ts is None:
        to_ts = now

    # Audit 17: reject an inverted range. from == to is a valid zero-width
    # window (returns no rows); from > to is malformed — a negative span would
    # otherwise fall through the downsample ladder and silently return empty.
    if to_ts < from_ts:
        return JSONResponse(
            status_code=422,
            content={"error": "'from' must be <= 'to'"},
        )

    # Validate requested columns against allowlist
    requested = [c.strip() for c in metrics.split(",") if c.strip()]
    invalid = [c for c in requested if c not in _deps._METRICS_COLS]
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

    conn = _deps.db()
    if bucket == 0:
        cols_sql = ", ".join(requested)
        sql = (
            f"SELECT ts, {cols_sql} FROM receiver_stats "
            f"WHERE ts BETWEEN ? AND ? ORDER BY ts"
        )
        rows = conn.execute(sql, (from_ts, to_ts)).fetchall()
    else:
        agg_cols = ", ".join(_deps._metrics_agg(c) for c in requested)
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


@router.get("/api/health")
def api_health() -> dict:
    try:
        _deps.db().execute("SELECT 1")
        db_ok = True
    except (sqlite3.Error, OSError):
        # STY-7: fail soft on a genuine DB-liveness failure (locked/corrupt
        # file, missing path) — but narrow to DB/OS errors so a non-DB
        # programming error surfaces as a 500 instead of a masked "degraded".
        db_ok = False
    from .vdl2 import vdl2_health
    return {"status": "ok" if db_ok else "degraded", "vdl2": vdl2_health()}


@router.get("/api/metrics/health")
def api_metrics_health() -> dict:
    cached = cache._get_cache("health")
    if cached is not None:
        return cached
    report = receiver_health.compute_health(_deps.db()).to_dict()
    cache._set_cache("health", report)
    return report
