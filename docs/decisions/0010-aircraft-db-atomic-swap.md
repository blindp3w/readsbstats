# Atomic aircraft_db swap via staging table

- Status: ACCEPTED
- Date: 2026-05-26 (revised 2026-05-31, BE-2; revised 2026-05-31, SQLite 3.45.x fix)

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

Five-step rename-rename-drop swap inside `update_aircraft_db()`:

1. **Recover** at the top of every call: `_recover_aborted_swap()`
   detects state left by a previous interrupted run (orphan
   `aircraft_db_old` or `aircraft_db_new`) and restores the canonical
   table before anything else proceeds.
2. **Build** a staging table `aircraft_db_new` (transient — created
   and dropped within the same function call, never persisted in DDL
   or `_migrate()`). The CREATE and the streaming chunked INSERTs run
   inside the swap transaction (see the 2026-05-31 SQLite 3.45.x
   revision under step 4). The *old* `aircraft_db` stays intact and
   queryable to WAL readers throughout this phase.
3. **Validate** the new row count against the previous count. If
   `new_count < AIRCRAFT_DB_MIN_RATIO × prev_count` (default 0.8),
   raise `RuntimeError` and refuse the swap. First-ever imports
   (`prev_count == 0`) bypass this check.
4. **Swap** via three statements inside **one explicit transaction**:
   ```
   BEGIN IMMEDIATE
   ALTER TABLE aircraft_db     RENAME TO aircraft_db_old
   ALTER TABLE aircraft_db_new RENAME TO aircraft_db
   DROP TABLE aircraft_db_old
   COMMIT          -- on any error: ROLLBACK
   ```
   **Revision (2026-05-31, BE-2):** the original ADR claimed Python's
   `sqlite3` "commits DDL immediately, so the swap cannot be wrapped in a
   transaction." That is **false** for Python ≥ 3.6 (the project requires
   ≥ 3.10): the implicit pre-DDL commit was removed, and SQLite DDL —
   including `ALTER TABLE … RENAME` — is fully transactional. The non-atomic
   rename-rename-drop was therefore a real durability hole: if the second
   rename failed, `aircraft_db` was left renamed away to `aircraft_db_old`
   (absent under its canonical name). We now wrap all three statements in a
   single transaction. Because the connection uses the legacy `sqlite3`
   transaction mode (which auto-begins only before DML, never before DDL),
   an explicit `BEGIN IMMEDIATE` is required to open the write transaction.
   In WAL mode concurrent readers keep seeing the old `aircraft_db` until
   `COMMIT`, so there is **no observable window** where the table is absent;
   an interrupted swap rolls back wholesale and leaves `aircraft_db` intact.

   **Revision (2026-05-31, SQLite 3.45.x fix):** the BE-2 revision kept the
   staging *build* (CREATE + chunked INSERT) in its own committed
   transactions, *outside* the swap transaction, to hold the write lock only
   for the three fast renames. That split broke on **SQLite 3.45.x** (the
   version shipped with Ubuntu 24.04 on the Pi): the freshly-`CREATE`d
   `aircraft_db_new`, committed in one transaction, was **not visible** to the
   next transaction's `INSERT` on the same connection — the full refresh
   aborted with `no such table: aircraft_db_new`. (Newer SQLite did not
   reproduce it, so local tests passed.) The CREATE, the chunked INSERTs, the
   count validation, and the rename-rename-drop now all run in **one**
   `BEGIN IMMEDIATE … COMMIT`, which guarantees the staging table is
   self-visible. Holding the write lock for the full ~620 k-row reload is
   acceptable here for two reasons that did not hold when A13-061 introduced
   the chunking: (a) `scripts/update.sh` **stops the collector** before
   running the updater, so there is no concurrent writer to cooperate with,
   and (b) in WAL mode a long write transaction never blocks readers (the web
   server). The connection still uses legacy `sqlite3` transaction mode, which
   auto-begins only before DML and never before DDL, so the explicit
   `BEGIN IMMEDIATE` is what opens the unit of work.
5. **Cleanup-on-failure** drops the orphaned `aircraft_db_new`. With the
   transactional swap, a mid-rename failure rolls back wholesale, so
   `aircraft_db` is never absent and `aircraft_db_old` is never left behind.
   `_recover_aborted_swap` is retained defensively — it still cleans up an
   orphan `aircraft_db_new` from a crashed *build* phase, and restores an
   `aircraft_db_old` left by a DB that crashed under the pre-revision code.

## Alternatives considered

- **One big transaction containing DELETE + all INSERTs (in place).**
  Restores atomicity but mutates the live `aircraft_db` in place, so a
  rollback still has to restore ~620 k rows and any reader in the same
  process snapshot sees an empty table mid-write. The staging-table
  build keeps the old table untouched until the rename, which the
  in-place variant cannot. (Note: the current design *does* now hold one
  transaction for the staging build + swap — see the 2026-05-31 SQLite
  3.45.x revision — but it builds the *new* table rather than mutating
  the live one, and the collector is stopped during the refresh, so the
  A13-061 lock-cooperation concern no longer applies.)
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
