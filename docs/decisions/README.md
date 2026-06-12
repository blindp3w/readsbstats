# Architecture Decision Records

This is the project's **decision log** — one record per significant architecture
decision, capturing its context, the decision taken, and its consequences.
Records follow the [Michael Nygard template](https://github.com/joelparkerhenderson/architecture-decision-record)
(Title · Status · Context · Decision · Consequences) and are numbered
sequentially. Where a decision was later refined, the update is added in place as
a date-stamped **Revision** note (see ADR-0010).

| # | Decision | Status | Date |
|---|---|---|---|
| [0001](0001-sqlite-wal-per-thread-connections.md) | SQLite WAL mode + per-thread connections in the web server | Accepted | 2026-05-09 |
| [0002](0002-duckdb-analytical-accelerator.md) | DuckDB analytical accelerator for aggregate scans | Superseded by ADR-0014 | 2026-05-17 |
| [0003](0003-flag-anonymous-computed-at-query-time.md) | FLAG_ANONYMOUS computed at query time, not stored | Accepted | 2026-05-13 |
| [0004](0004-http-safe-ssrf-guard.md) | Centralised SSRF guard via http_safe.py | Accepted | 2026-05-11 |
| [0005](0005-notification-dispatch-queue.md) | Single long-lived Telegram dispatch consumer thread | Accepted | 2026-05-11 |
| [0006](0006-nginx-direct-static-serving.md) | nginx serves SPA static assets directly (bypassing FastAPI) | Accepted | 2026-05-17 |
| [0007](0007-sqlite-integrity-checks.md) | SQLite crash-safety hardening: `synchronous=FULL` + integrity checks | Accepted | 2026-05-19 (rev. 2026-06-11: `synchronous=FULL` superseded) |
| [0008](0008-apache-echarts-frontend-charts.md) | Apache ECharts as the SPA chart library | Accepted | 2026-05-19 |
| [0009](0009-maplibre-gl-frontend-map.md) | MapLibre GL as the SPA map library | Accepted | 2026-05-23 |
| [0010](0010-aircraft-db-atomic-swap.md) | Atomic `aircraft_db` swap via staging table | Accepted | 2026-05-26 (rev. 2026-05-31) |
| [0011](0011-positions-endpoints-split.md) | Split `/api/flights/{id}` positions into three endpoints | Accepted | 2026-05-26 |
| [0012](0012-coalesce-drop-deferred.md) | Defer COALESCE drop in flight filter SQL | Accepted | 2026-05-26 |
| [0013](0013-vdl2-timeseries-reuses-metrics-columnar-contract.md) | VDL2 reception charts reuse the `/api/metrics` columnar contract | Accepted | 2026-06-05 |
| [0014](0014-daily-rollups-replace-duckdb.md) | Daily rollup tables replace the DuckDB accelerator | Accepted | 2026-06-11 |
| [0015](0015-scaled-integer-positions-storage.md) | Scaled-integer storage for the positions table (schema v6) | Accepted | 2026-06-11 |
| [0016](0016-oooi-qseries-parsing.md) | OOOI from Q-series compact reports (and what we deliberately don't parse) | Accepted | 2026-06-12 |

## Adding an ADR

1. Create `00NN-short-topic.md`, numbered next in sequence.
2. Use the Nygard sections: Title · Status · Context · Decision · Consequences.
3. Add a row to the table above.
