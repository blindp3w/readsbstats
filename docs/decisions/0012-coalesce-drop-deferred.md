# Defer COALESCE drop in flight filter SQL

- Status: ACCEPTED
- Date: 2026-05-26

## Context

The History page filters use
`COALESCE(f.registration, adb.registration) LIKE ?` and
`COALESCE(f.aircraft_type, adb.type_code) = ?` so a search hits both
the flight row's stored value and a fresh `aircraft_db` lookup when
the stored value is NULL. A 2026 performance review flagged this as
index-unfriendly: the existing single-column indexes
(`idx_flights_registration`, `idx_flights_type`) cannot satisfy the
predicate because the expression wraps the column in a function call.

The audit's proposed fix is to store the effective registration / type
on `flights` at open or enrichment time, so the WHERE clause becomes
a direct column predicate. The collector already does this for new
flights; the missing piece is a backfill for historical rows.

## Decision

Ship the backfill **without** dropping the COALESCE. Specifically:

1. Add `_backfill_flights_enrichment()` to
   `database.run_background_migrations()` — populates
   `flights.registration` / `flights.aircraft_type` from
   `aircraft_db` via correlated subqueries (both `flights` and
   `aircraft_db` expose a `registration` column, so `UPDATE … FROM`
   raises "ambiguous column name" — correlated subqueries avoid it).
2. Run the same UPDATEs in `db_updater.update_aircraft_db()` after
   the atomic swap so newly-known ICAOs back-apply to historical
   NULL-registration flights.
3. **Keep** the `COALESCE(...)` wrappers in
   `_build_flight_filter()` for now. A follow-up PR will
   drop them once production has confirmed the backfill has
   completed.

## Why not drop in the same release

The background migration on a 10M-positions / 35 k-flights Pi DB
runs for several minutes. During that window the web process has
already deployed; if we'd dropped the COALESCE in the same release,
filtered searches against not-yet-backfilled rows would return empty.
The COALESCE provides a real-time fallback that the backfill has
yet to make redundant.

## Follow-up gate (not yet taken)

The COALESCE wrappers are still in place — `_build_flight_filter()` now
lives in `src/readsbstats/api/_deps.py` after the router split — and
dropping them remains a deferred optimization. Prerequisites before the
drop:

- Production `journalctl -u readsbstats` confirms
  `_backfill_flights_enrichment` has run to completion.
- A spot check of
  `SELECT COUNT(*) FROM flights WHERE registration IS NULL AND EXISTS (SELECT 1 FROM aircraft_db WHERE icao_hex = flights.icao_hex)`
  returns 0.

## Consequences

- The eventual COALESCE drop unlocks
  `idx_flights_registration` and `idx_flights_type` for the
  registration/type LIKE/= predicates — faster filtered History
  pages.
- The dual write (backfill in background migration + post-swap)
  means the post-swap UPDATEs run during every weekly updater run
  forever, not just once. Cost is bounded by the number of
  NULL-registration flights that pick up an `aircraft_db` row this
  week (typically < 100).
