# FLAG_ANONYMOUS computed at query time, not stored

- Status: ACCEPTED
- Date: 2026-05-13

## Context

`FLAG_ANONYMOUS=16` marks aircraft whose 24-bit Mode-S address falls outside every ICAO state-allocated block — non-ICAO hex contacts that are typically military/OPSEC, TIS-B rebroadcasts, or MLAT-synthetic identifiers. The ICAO allocation table (`icao_ranges._RAW`) is community-sourced and will gain new entries over time as gaps are found.

## Decision

`FLAG_ANONYMOUS` is computed at SQL query time via `icao_ranges.anonymous_flag_sql()` — a CASE expression embedded in every flag-projecting query. It is never stored in any database column or cached anywhere.

## Consequences

- Adding a missing state allocation to `_RAW` retroactively reclassifies every historical flight on the next query — no backfill or migration needed.
- The CASE expression (~10 KB of SQL) is embedded in every flag-projecting query. SQLite's prepared-statement cache amortises parsing cost across calls.
- Queries use `_FLAGS_EXPR_F` / `_FLAGS_EXPR_SUB` / `_FLAGS_EXPR_AF` variants matching the `icao_hex` column alias in scope. Use these, not ad-hoc expressions.
- The GS-physics cap in `web.py` deliberately uses the bare `(adb.flags | axo.flags)` expression — the anon bit doesn't affect the max-GS limit.
