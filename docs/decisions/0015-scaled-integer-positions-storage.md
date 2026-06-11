# Scaled-integer storage for the positions table (schema v6)

- Status: ACCEPTED
- Date: 2026-06-11

## Context

A 2026-06-11 production-dump analysis (6.18 M `positions` rows, 65 days) showed the
1.3 GB `history.db` was 94 % positions data: ~519 MB of table plus what was originally
~800 MB of indexes. The table stored every numeric sample as SQLite `REAL` — a flat
8 bytes per value — even though the underlying ADS-B data carries far less precision:

- CPR-encoded positions resolve to roughly 5 m; storing latitude/longitude at 1e-5°
  (~1.1 m) loses nothing.
- Ground speed, track, and RSSI arrive with at most one decimal of useful precision.
- `source_type` repeated the same handful of strings (`adsb_icao` ×4.84 M, `mlat`
  ×1.34 M) — ~48 MB of duplicate text.
- The `messages` column was written on every insert and read by nothing.

SQLite stores small integers in 1–4 bytes, so quantizing to integers roughly halves
the table. The operator approved the precision trade-off explicitly.

## Decision

Schema version 6 rebuilds `positions` with scaled-integer columns:

| Column | Encoding | Precision |
|---|---|---|
| `lat`, `lon` | `round(deg × 1e5)` | ~1.1 m |
| `gs`, `track`, `rssi` | `round(val × 10)` | 0.1 kt / 0.1° / 0.1 dB |
| `alt_baro`, `alt_geom`, `baro_rate` | plain INTEGER (unchanged) | 1 ft / 1 ft/min |
| `source` | small integer code | lossless for every type the collector classifies |
| `messages` | **dropped** | write-only column, no reader |

All encoding/decoding is centralized in `src/readsbstats/posenc.py` — call sites never
hand-roll `× 100000`. Reads decode in SQL (`lat / 100000.0 AS lat`), so NULLs propagate
and **API responses are byte-compatible with v5** (same field names including
`source_type`, same float units). `AUTOINCREMENT` is dropped from the primary key
(oldest-first retention never frees the max rowid, so plain rowid assignment cannot
reuse ids).

Migration: `scripts/migrate_v6.py` rebuilds the table offline (FK check, `ANALYZE`,
final `VACUUM` that also reclaims the space freed by Phases 1–2). `update.sh` runs it
automatically when it detects `schema_version < 6`, with both services stopped.
Databases with ≤200 000 rows migrate inline in `_migrate()` at the next service start.

## Consequences

- **−50 % positions table** (519 → 259 MB measured); the whole positions footprint
  (table + 2 indexes) drops from ~1 280 MB pre-overhaul to ~435 MB. Expected file size
  after the migration's VACUUM: ~0.5 GB.
- Smaller rows mean proportionally less WAL churn and checkpoint write-back on the
  Pi's USB HDD — the write path touches 3 B-trees per insert, each with ~half the bytes.
- Values are quantized once at encode time; decode returns the quantized value
  exactly, so there is no cumulative drift. Aggregates recomputed from positions
  (purge crossing-flights, GS outlier rescans) may shift by ≤0.05 unit / ≤5e-6° versus
  their feed-precision originals — far below display precision.
- Corrupt-feed values that would overflow the integer encoding are nulled at ingestion
  (`track` range-checked to 0–360°, `rssi` to −200…100 dB) with an int64 guard in the
  encoders as a second layer.
- Python `round()` (banker's) and the migration SQL's `ROUND()` (half-away-from-zero)
  can differ by one least-significant unit on exact halves — a measure-zero,
  display-invisible discrepancy documented in `posenc`.
