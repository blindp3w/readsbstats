"""Aggregate statistics, personal records, polar range plot."""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Query

from .. import cache, config, icao_ranges, schemas
from . import _deps


router = APIRouter()


@router.get("/api/stats", response_model=schemas.StatsResponse,
            response_model_exclude_unset=True)
def api_stats(
    from_ts: int | None = Query(None, alias="from"),
    to_ts:   int | None = Query(None, alias="to"),
) -> dict:
    filtered = from_ts is not None or to_ts is not None
    cache_key = "stats" if not filtered else f"stats:{from_ts}:{to_ts}"
    cached = cache._get_cache(cache_key)
    if cached is not None:
        return cached
    if filtered:
        # Filtered windows are cheap (index-scoped) and quantized by the SPA —
        # compute lock-free.
        result = _compute_stats_sync(from_ts, to_ts)
        cache._set_cache(cache_key, result)
        return result
    # All-time is the expensive path. Serialize it under _stats_compute_lock so
    # a burst of concurrent "All time" requests (or an overlap with the hourly
    # prewarmer) computes once; the rest wait and reuse the freshly cached result.
    with cache._stats_compute_lock:
        cached = cache._get_cache(cache_key)
        if cached is not None:
            return cached
        result = _compute_stats_sync(from_ts, to_ts)
        cache._set_cache(cache_key, result)
        return result


def _compute_stats_sync(from_ts: int | None, to_ts: int | None) -> dict:
    """Build the full ``/api/stats`` payload (every sub-query).

    Extracted from ``api_stats`` so the cache prewarmer can warm the all-time
    payload (``from_ts=to_ts=None``) on a background thread. Pure reads plus
    ``os.path.getsize`` — never writes ``history.db``; only the caller mutates
    the in-memory response cache.

    The unfiltered (all-time) payload embeds ``now``-relative fields
    (``flights_last_24h``, ``trends``, the 24 h new-aircraft cutoff) computed at
    call time; when served from the prewarmed cache they reflect warm time
    (up to ~1 h stale at the prewarm cadence). Acceptable — all-time is an
    opt-in lifetime view and the page defaults to 7d.
    """
    filtered = from_ts is not None or to_ts is not None
    conn = _deps.db()
    now = int(time.time())
    cutoff_24h      = now - 86400
    cutoff_7d       = now - 7 * 86400
    cutoff_30d      = now - 30 * 86400
    cutoff_prev_24h = now - 2 * 86400
    cutoff_prev_7d  = now - 14 * 86400

    # WHERE fragments injected into all range-aware queries. BE-16: built from
    # the shared half-open _build_date_filter so stats match history/export
    # ([from, to) — a flight at exactly `to` is excluded).
    if filtered:
        ts_lo = from_ts if from_ts is not None else 0
        ts_hi = to_ts   if to_ts   is not None else now
        _dc,  _fp_list = _deps._build_date_filter(ts_lo, ts_hi, col="first_seen")
        _djc, _        = _deps._build_date_filter(ts_lo, ts_hi, col="f.first_seen")
        _dsql  = " AND ".join(_dc)    # first_seen >= ? AND first_seen < ?
        _djsql = " AND ".join(_djc)   # f.first_seen >= ? AND f.first_seen < ?
        _fw   = "WHERE " + _dsql      # standalone, unaliased
        _fwa  = "AND "   + _dsql      # appended to existing WHERE, unaliased
        _fjw  = "WHERE " + _djsql     # standalone, aliased table
        _fjwa = "AND "   + _djsql     # appended to existing WHERE, aliased table
        _fp   = tuple(_fp_list)
    else:
        _fw = _fwa = _fjw = _fjwa = ""
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
                -- Audit 2026-06-01 S: half-open [lo, hi) — matches
                -- _build_date_filter and excludes double-counting at the
                -- per-second cutoff boundary.
                SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END)                  AS flights_24h,
                SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END)                  AS flights_7d,
                SUM(CASE WHEN first_seen >= ? AND first_seen < ? THEN 1 ELSE 0 END) AS flights_prev_24h,
                SUM(CASE WHEN first_seen >= ? AND first_seen < ? THEN 1 ELSE 0 END) AS flights_prev_7d,
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
                -- Audit 2026-06-01 S: half-open [lo, hi) — see main block above.
                SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END)                     AS flights_24h,
                SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END)                     AS flights_7d,
                SUM(CASE WHEN first_seen >= ? AND first_seen < ? THEN 1 ELSE 0 END) AS flights_prev_24h,
                SUM(CASE WHEN first_seen >= ? AND first_seen < ? THEN 1 ELSE 0 END) AS flights_prev_7d
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

    # Previous-window deltas. Frontend KPI cards have a `prev` slot that
    # was previously only fed by `trends.flights_*_prev` (24h/7d only),
    # leaving every other range with empty em-dashes. Compute totals over
    # `[ts_lo - D, ts_lo]` where D = current window length so Flights /
    # Unique aircraft / Position fixes all show a real comparison. Skipped
    # for unfiltered (all-time) since there's no equivalent prior window.
    previous_window: dict | None = None
    if filtered:
        window_seconds = ts_hi - ts_lo
        if window_seconds > 0:
            prev_lo = ts_lo - window_seconds
            prev_hi = ts_lo
            # Half-open `[prev_lo, prev_hi)` (BE-16): the upper bound is
            # exclusive so a flight whose first_seen lands on the boundary
            # second `ts_lo` belongs to the current window only, never to
            # both. This matches the current-window filter, which is also
            # half-open via `_build_date_filter()`.
            prev_agg = conn.execute(
                """
                SELECT
                    COUNT(*)                  AS total_flights,
                    SUM(total_positions)      AS total_positions,
                    COUNT(DISTINCT icao_hex)  AS unique_aircraft
                FROM flights
                WHERE first_seen >= ? AND first_seen < ?
                """,
                (prev_lo, prev_hi),
            ).fetchone()
            previous_window = {
                "from_ts":         prev_lo,
                "to_ts":           prev_hi,
                "total_flights":   prev_agg["total_flights"] or 0,
                "total_positions": prev_agg["total_positions"] or 0,
                "unique_aircraft": prev_agg["unique_aircraft"] or 0,
            }

    try:
        db_size = os.path.getsize(config.DB_PATH)
    except OSError:
        db_size = None

    # Lifetime block — receiver-wide totals that are NOT scoped to the
    # selected window. The Statistics page's "About this receiver" footer
    # reads from this so it stays stable when the user changes the range
    # picker. When `filtered=False` the main `agg` query is ALREADY over
    # all flights, so we reuse those values; when filtered, a small
    # extra aggregation runs.
    if not filtered:
        lifetime_total_flights    = agg["total_flights"]
        # COALESCE NULL → 0: SUM() returns NULL on an empty `flights`
        # table; the StatsResponse TS interface declares this as `number`
        # (not nullable), so coerce here rather than lie about the type.
        lifetime_total_positions  = agg["total_positions"] or 0
        lifetime_unique_aircraft  = agg["unique_aircraft"]
        lifetime_unique_airlines  = agg["unique_airlines"]
        lifetime_oldest_flight    = agg["oldest_flight"]
        lifetime_adsb_pct         = adsb_pct
        lifetime_mlat_pct         = mlat_pct
    else:
        life = conn.execute(
            """
            SELECT
                COUNT(*)                                                          AS total_flights,
                SUM(total_positions)                                              AS total_positions,
                COUNT(DISTINCT icao_hex)                                          AS unique_aircraft,
                COUNT(DISTINCT CASE
                    WHEN callsign IS NOT NULL AND callsign != '' AND length(callsign) >= 3
                    THEN substr(callsign,1,3) END)                                AS unique_airlines,
                MIN(first_seen)                                                   AS oldest_flight,
                ROUND(100.0 * SUM(adsb_positions) / NULLIF(SUM(total_positions),0), 1) AS adsb_pct,
                ROUND(100.0 * SUM(mlat_positions) / NULLIF(SUM(total_positions),0), 1) AS mlat_pct
            FROM flights
            """,
        ).fetchone()
        lifetime_total_flights    = life["total_flights"]
        # See note in the unfiltered branch — SUM() can return NULL.
        lifetime_total_positions  = life["total_positions"] or 0
        lifetime_unique_aircraft  = life["unique_aircraft"]
        lifetime_unique_airlines  = life["unique_airlines"]
        lifetime_oldest_flight    = life["oldest_flight"]
        lifetime_adsb_pct         = life["adsb_pct"] or 0
        lifetime_mlat_pct         = life["mlat_pct"] or 0
    lifetime_other_pct = round(100.0 - lifetime_adsb_pct - lifetime_mlat_pct, 1)

    # Military + interesting — one JOIN pass (OR-merge tar1090-db + ADSBx flags)
    flags_row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN ({_deps._FLAGS_EXPR_F} & 1) = 1 THEN 1 ELSE 0 END) AS military,
            SUM(CASE WHEN ({_deps._FLAGS_EXPR_F} & 2) = 2
                      AND ({_deps._FLAGS_EXPR_F} & 1) = 0 THEN 1 ELSE 0 END) AS interesting,
            SUM(CASE WHEN ({_deps._FLAGS_EXPR_F} & 16) = 16
                      AND ({_deps._FLAGS_EXPR_F} & 3)  = 0 THEN 1 ELSE 0 END) AS anonymous
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        {_fjw}
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
    # PY-2 (Audit 2026-05-31): include adsbx_overrides so types known only
    # via adsbx still appear in the stats panel and match what /api/flights
    # displays. The flight-row → aircraft_db → adsbx_overrides priority
    # mirrors _ENRICH_TYPE used everywhere else.
    top_types = conn.execute(
        f"""
        SELECT
            {_deps._ENRICH_TYPE}  AS type,
            {_deps._ENRICH_DESC}   AS type_desc,
            COUNT(*)              AS flights,
            COUNT(DISTINCT f.icao_hex) AS unique_aircraft
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        WHERE {_deps._ENRICH_TYPE} IS NOT NULL {_fjwa}
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
            ORDER BY day ASC
            LIMIT 31
            """,
            # LIMIT 31 not 30: cutoff_30d is `now - 30*86400` (an instant,
            # not a day boundary), so the WHERE typically straddles 31
            # distinct UTC date strings (partial start day + 30 full days
            # ending today). With ASC ordering, LIMIT 30 would truncate
            # today's bar — the most user-relevant one.
            (cutoff_30d,),
        ).fetchall()
    else:
        daily = conn.execute(
            f"""
            SELECT date(first_seen, 'unixepoch') AS day,
                   COUNT(DISTINCT icao_hex) AS unique_aircraft,
                   COUNT(*) AS flights
            FROM flights
            {_fw}
            GROUP BY day
            ORDER BY day ASC
            """,
            _fp,
        ).fetchall()

    # New aircraft — first seen within the window
    if not filtered:
        new_having   = "HAVING MIN(f.first_seen) > ?"
        new_cnt_sql  = "SELECT COUNT(*) FROM (SELECT icao_hex FROM flights GROUP BY icao_hex HAVING MIN(first_seen) > ?)"
        new_params   = (cutoff_24h,)
    else:
        # BE-16: half-open [ts_lo, ts_hi) to match the rest of the stats range.
        new_having   = "HAVING MIN(f.first_seen) >= ? AND MIN(f.first_seen) < ?"
        new_cnt_sql  = "SELECT COUNT(*) FROM (SELECT icao_hex FROM flights GROUP BY icao_hex HAVING MIN(first_seen) >= ? AND MIN(first_seen) < ?)"
        new_params   = (ts_lo, ts_hi)

    new_aircraft_rows = conn.execute(
        f"""
        SELECT sub.icao_hex,
               COALESCE(f2.registration, adb.registration, axo.registration) AS registration,
               COALESCE(f2.aircraft_type, adb.type_code, axo.type_code)     AS aircraft_type,
               COALESCE(adb.type_desc, axo.type_desc, '')                   AS type_desc,
               {_deps._FLAGS_EXPR_SUB}                                      AS flags,
               sub.first_seen_ever
        FROM (
            SELECT f.icao_hex, MIN(f.first_seen) AS first_seen_ever
            FROM flights f
            GROUP BY f.icao_hex
            {new_having}
            ORDER BY MIN(f.first_seen) DESC
            LIMIT 10
        ) sub
        -- BE-15 (Audit 2026-05-31): two flights for one ICAO can share the
        -- exact first_seen, so an equality self-join would match both and emit
        -- a duplicate row with a non-deterministic reg/type.  Resolve to a
        -- single representative (highest id among the earliest-first_seen
        -- flights) via a correlated pick.
        LEFT JOIN flights f2 ON f2.id = (
            SELECT f3.id FROM flights f3
            WHERE f3.icao_hex = sub.icao_hex
              AND f3.first_seen = sub.first_seen_ever
            ORDER BY f3.id DESC LIMIT 1
        )
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
               {_deps._FLAGS_EXPR_F}                                        AS flags,
               COUNT(*)                                    AS flights,
               MAX(f.last_seen)                            AS last_seen
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        {_fjw}
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

    # Furthest detected aircraft. Sprint 1 #4: surface the record-set
    # timestamp under the explicit `record_set_at` key so the frontend
    # MaxRangeCard sublabel can render `{callsign} · set {date}`. This is
    # the `first_seen` of the flight that holds the max-distance record
    # (the flight could span hours; `first_seen` is when it started).
    furthest_row = conn.execute(
        f"""
        SELECT {_deps._FLIGHT_COLS}
        FROM flights f {_deps._FLIGHT_JOIN}
        WHERE f.max_distance_nm IS NOT NULL {_fjwa}
        ORDER BY f.max_distance_nm DESC
        LIMIT 1
        """,
        _fp,
    ).fetchone()
    if furthest_row:
        furthest = dict(furthest_row)
        # Rename, don't duplicate: keep a single timestamp key in the
        # response so a future cleanup of `first_seen` from the projection
        # doesn't leave a stale alias behind. No external consumer reads
        # the original `first_seen` from `furthest_aircraft` — the
        # Personal Records section uses `/api/stats/records` instead.
        furthest["record_set_at"] = furthest.pop("first_seen", None)
    else:
        furthest = None

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
        "anonymous_flights":       flags_row["anonymous"]    or 0,
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
        "previous_window": previous_window,
        "lifetime": {
            "total_flights":    lifetime_total_flights,
            "total_positions":  lifetime_total_positions,
            "unique_aircraft":  lifetime_unique_aircraft,
            "unique_airlines":  lifetime_unique_airlines,
            "oldest_flight":    lifetime_oldest_flight,
            "db_size_bytes":    db_size,
            "source_breakdown": {
                "adsb":  lifetime_adsb_pct,
                "mlat":  lifetime_mlat_pct,
                "other": lifetime_other_pct,
            },
        },
        "heatmap": [dict(r) for r in heatmap_rows],
        "top_countries": top_countries,
        "frequent_aircraft": [dict(r) for r in frequent_aircraft],
        "top_routes":   [dict(r) for r in top_routes],
        "top_airports": [dict(r) for r in top_airports],
        "range":  {"from": from_ts, "to": to_ts} if filtered else None,
    }
    return result


@router.get("/api/stats/records")
def api_stats_records() -> dict:
    """All-time personal records: furthest / fastest / highest / longest flight."""
    cached = cache._get_cache("records")
    if cached is not None:
        return cached

    conn = _deps.db()

    # Audit-13 A13-040: previously accepted any string as `order_col` and
    # f-stringed it into SQL — latent SQLi if a future caller forwarded a
    # query param. Explicit allowlist enforced at function entry via
    # _deps._assert_top1_column.
    def _top1(order_col: str, extra_where: str = "", extra_params: tuple = ()) -> dict | None:
        _deps._assert_top1_column(order_col)
        row = conn.execute(
            f"""
            SELECT {_deps._FLIGHT_COLS}
            FROM flights f {_deps._FLIGHT_JOIN}
            WHERE f.{order_col} IS NOT NULL {extra_where}
            ORDER BY f.{order_col} DESC
            LIMIT 1
            """,
            extra_params,
        ).fetchone()
        return dict(row) if row else None

    furthest = _top1("max_distance_nm")
    # Audit-13 A13-050: parameterise MAX_GS_* numerics (previously f-stringed).
    fastest  = _top1(
        "max_gs",
        "AND f.max_gs <= CASE WHEN (COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 1 != 0"
        " THEN ? ELSE ? END",
        (config.MAX_GS_MILITARY_KTS, config.MAX_GS_CIVIL_KTS),
    )
    highest  = _top1("max_alt_baro")

    longest_row = conn.execute(
        f"""
        SELECT {_deps._FLIGHT_COLS}, (f.last_seen - f.first_seen) AS duration_s
        FROM flights f {_deps._FLIGHT_JOIN}
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
    cache._set_cache("records", result)
    return result


@router.get("/api/stats/polar")
def api_stats_polar() -> dict:
    """Max detection range per 10° azimuth bucket (36 buckets, 0 = North)."""
    cached = cache._get_cache("polar")
    if cached is not None:
        return cached

    # BUCKET_DEG controls angular resolution of the polar plot.
    # 10° (36 buckets) is a good default.
    BUCKET_DEG = 10
    n_buckets = 360 // BUCKET_DEG

    # Use precomputed max_distance_bearing from flights table (much faster
    # than scanning millions of position rows with trig math).
    sql_rows = _deps.db().execute(
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
    cache._set_cache("polar", result)
    return result
