# DuckDB analytical accelerator for aggregate scans

- Status: SUPERSEDED by [ADR-0014](0014-daily-rollups-replace-duckdb.md)
- Date: 2026-05-17

## Context

A handful of FastAPI endpoints run full-table aggregate scans over the `positions` table:
`/api/map/heatmap`, `/api/map/coverage`, `/api/stats/polar`.

At 3–5 M positions/week, a 30-day window is 15–25 M rows. SQLite is single-threaded per query
and has no index that can serve `GROUP BY round(lat, p), round(lon, p)`. On the Pi 4,
`?window=30d` exceeded nginx's 60-second `proxy_read_timeout` and returned 504.

## Decision

Add an opt-in DuckDB analytical accelerator (`RSBS_USE_DUCKDB=1`). DuckDB reads the live SQLite
file directly via its `sqlite_scanner` extension — no data duplication, no separate write path,
no migration. Every query is wrapped in `try/except` with automatic SQLite fallback, so enabling
DuckDB can never regress an endpoint.

All writes stay SQLite. DuckDB is a query-time accelerator only.

Two engine-quirks are documented in `src/readsbstats/analytics.py`:
- `CAST(double AS INTEGER)` rounds in DuckDB, truncates in SQLite — coverage SQL uses `FLOOR()::INTEGER`.
- `round(x, n)` is banker's rounding in DuckDB vs. half-up in SQLite — ≤0.01 % of cell-boundary
  points may bucket differently (cosmetic).

DuckDB requires a writable home directory for its extension cache. The `readsbstats` system user
has no `/home`, so `RSBS_DUCKDB_HOME_DIR` (default `/mnt/ext/readsbstats/duckdb-home`) points
DuckDB at a writable location before `INSTALL sqlite_scanner`.

## Consequences

- Heatmap `?window=30d` drops from 60 s+ (nginx 504) to ~5 s cold; subsequent hits are
  41 ms from the in-process cache.
- The prewarmer thread (`_prewarm_loop`) keeps all 8 cache entries warm so users rarely see cold paths.
- `analytics.py` centralises all DuckDB SQL — if a column changes, one file to update.
- Adds `duckdb==1.5.2` (~30 MB) as an optional dependency; absent if `RSBS_USE_DUCKDB=0`.
- `update.sh` runs `INSTALL sqlite_scanner` pre-flight after each deploy so the extension
  is always available when the flag is on.
