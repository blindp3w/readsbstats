# Atomic aircraft_db swap via staging table

- Status: ACCEPTED
- Date: 2026-05-26

## Context

`db_updater.update_aircraft_db()` historically committed
`DELETE FROM aircraft_db` in its own transaction, then bulk-inserted
~620 k rows in chunked transactions (Audit-13 A13-061 introduced
the chunking so the writer lock would release between chunks and let
the collector interleave). The audit on 2026-05-26 flagged this as a
durability hole: any failure between the DELETE commit and the final
INSERT chunk left `aircraft_db` empty or partially populated, and
enrichment/flags/photo fallbacks degraded silently until the next
weekly run.

A truncated upstream — e.g. a `200 OK` whose gzip body was cut short
by a midstream network hiccup — produced the same outcome: the parse
loop emitted fewer rows than expected and the table shrank
catastrophically with no signal that anything was wrong.

## Decision

Three-step atomic swap inside `update_aircraft_db()`:

1. **Build** a staging table `aircraft_db_new` (transient — created
   and dropped within the same function call, never persisted in DDL
   or `_migrate()`). Streaming INSERTs run in chunked transactions
   exactly as before, so the writer-lock cooperation Audit-13 A13-061
   established is preserved. The *old* `aircraft_db` stays intact and
   queryable throughout this phase.
2. **Validate** the new row count against the previous count. If
   `new_count < AIRCRAFT_DB_MIN_RATIO × prev_count` (default 0.8),
   raise `RuntimeError` and refuse the swap. First-ever imports
   (`prev_count == 0`) bypass this check.
3. **Swap** with a brief two-statement transaction:
   `DROP TABLE aircraft_db; ALTER TABLE aircraft_db_new RENAME TO aircraft_db`.

A bare-except cleanup drops `aircraft_db_new` on failure so a crashed
run does not leave the staging table behind to confuse the next one.

## Alternatives considered

- **One big transaction containing DELETE + all INSERTs.** Restores
  atomicity but holds the writer lock for the full ~620 k-row reload
  — several seconds on the Pi 4 — re-introducing the very lock
  contention Audit-13 A13-061 was added to fix.
- **`UPSERT` per row keyed on `icao_hex`.** Avoids the swap entirely
  but cannot detect deletions on the upstream side; rows removed from
  tar1090-db would linger forever in the local cache.
- **Two databases, atomic file rename.** Cleanest but requires
  rewriting the connection plumbing in `web.py` and `collector.py`,
  both of which open `aircraft_db` against the same `history.db`
  handle. Out of scope.

## Consequences

- Failures preserve the last-known-good `aircraft_db`. Enrichment
  keeps working until the next successful run.
- The relative-size floor catches truncated downloads. The threshold
  is conservative; legitimate upstream contractions (tar1090-db
  removing duplicate airframes) will require a manual one-off override
  via `RSBS_AIRCRAFT_DB_MIN_RATIO=0.5` or similar.
- Operators should monitor `journalctl -u readsbstats-updater` for
  the `aircraft_db swap refused` message — the swap fail-safe will
  block updates until the upstream recovers or the operator tunes the
  ratio.
