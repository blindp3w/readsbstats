# Daily rollup tables replace the DuckDB accelerator

- Status: ACCEPTED
- Date: 2026-06-11

## Context

ADR-0002 added an opt-in DuckDB analytical accelerator (`RSBS_USE_DUCKDB=1`) to speed up
`/api/map/heatmap` and `/api/map/coverage`. Those two endpoints ran full `positions` table
aggregate scans — 15–25 M rows for a 30-day window on a Pi 4 with a USB HDD, reliably
exceeding the nginx 60-second `proxy_read_timeout` (504). DuckDB's sqlite_scanner ATTACH
brought the cold query from ~30 s to ~3 s.

The trade-off was significant:
- **256 MB working-set cap** — the largest resident allocation on the Pi, dwarfing all
  other processes.
- **sqlite_scanner extension download** — ~5 s HTTPS fetch from extensions.duckdb.org on
  first use per deploy (pre-cached by `scripts/update.sh` but still a hard dependency on
  an external ARM-compatible binary).
- **Silent-degradation failure mode** — if the extension or ATTACH fails, all 8
  heatmap/coverage cache entries silently fall through to the raw SQLite path, which
  exceeds the nginx timeout and serves 504s.
- **ARM binary maintenance** — DuckDB releases do not always publish an ARM64 extension
  binary in the same build; any version bump required manual verification.

A 2026-06-11 production-dump analysis confirmed that collector-maintained daily rollup
tables answer the same aggregate windows in under 100 ms:

- `grid_daily (scale, day, lat_b, lon_b, w)` — ~57 000 rows at scale=10 (0.1° cells)
  cover the entire history. The heatmap `?window=7d` / `30d` / `all` queries read a few
  thousand of those rows and GROUP BY in milliseconds.
- `coverage_daily (day, bearing_b, max_nm)` — one row per degree-bearing per day; the
  coverage endpoint rebuckets 1° → 10° in SQL (integer division).

Both rollups are maintained inside the collector's existing poll transaction — zero
additional write latency. The `24h` window keeps exact rolling semantics over raw
`positions` (ranged ts scan, which is small and fast with the existing
`idx_positions_flight_ts` index).

## Decision

Replace the DuckDB accelerator with collector-maintained daily rollup tables:

- `grid_daily` at two scales (10 = 0.1°, 100 = 0.01°) covers `?window=30d/all` and
  `?window=7d` respectively; the collector upserts aggregated buckets per poll in the
  same write transaction.
- `coverage_daily` at 1° bearing resolution covers all ≥7d coverage windows.
- `?window=24h` continues to use a raw `positions` scan for exact rolling semantics.
- While the one-time historical backfill is in progress (`rollups_ready` flag unset in
  `meta`), all windows fall back to the raw `positions` scan — the same path used before
  ADR-0002.

DuckDB (`duckdb==1.5.3`), all `RSBS_USE_DUCKDB` / `RSBS_DUCKDB_*` config vars,
`src/readsbstats/analytics.py`, and the `update.sh` pre-cache section are removed.

The cache prewarmer (`cache._start_prewarmer()`) warms all targets unconditionally — the
`include_map` parameter and DuckDB gating are removed. ≥7d map targets are cheap from
rollups; the prewarmer no longer risks running expensive raw scans in the background.

## Consequences

- **−256 MB** process working set on the Pi 4.
- **−2 ts-composite indexes** (`idx_positions_ts_flight`, `idx_positions_ts_lat_lon`) —
  ~320 MB saved on the USB HDD. These are dropped after backfill in `run_background_migrations()`.
- **≥7d windows are day-quantized** — the rollup `day` column is `ts // 86400`, so the
  7d window includes "last 7 full days + today so far" rather than the exact rolling 7 ×
  86 400-second window. This is imperceptible on the heatmap and coverage polygon.
- **Rollup history survives raw-position retention** — once daily rollups exist, a future
  purge of raw `positions` rows older than N days does not degrade the heatmap/coverage
  response for longer windows.
- **Identical FLOOR bucketing** — `grid_daily.lat_b` / `lon_b` use `FLOOR(lat * scale +
  0.5)` which matches the raw SQLite path exactly (no inter-engine rounding differences
  to manage; the DuckDB banker's-rounding workaround in `analytics.py` is no longer needed).
- **ADR-0002 is superseded** — that record is left in place for historical context.
