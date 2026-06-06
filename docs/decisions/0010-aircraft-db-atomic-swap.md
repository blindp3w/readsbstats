# Atomic aircraft_db swap via staging table

- Status: ACCEPTED
- Date: 2026-05-26 (revised 2026-05-31: backend hardening, SQLite 3.45.x fix, v2.12.2 timer-path orchestration)

## Context

`db_updater.update_aircraft_db()` historically committed
`DELETE FROM aircraft_db` in its own transaction, then bulk-inserted
~620 k rows in chunked transactions (a prior review introduced
the chunking so the writer lock would release between chunks and let
the collector interleave). A 2026 durability review flagged this as a
hole: any failure between the DELETE commit and the final
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
   **Revision (2026-05-31, backend hardening):** the original ADR claimed Python's
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

   **Revision (2026-05-31, SQLite 3.45.x fix):** the backend-hardening revision kept the
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
   acceptable here for two reasons that did not hold when the chunking was
   first introduced: (a) `scripts/update.sh` **stops the collector** before
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
  transaction for the staging build + swap — see the later single-transaction
  SQLite 3.45.x revision — but it builds the *new* table rather than mutating
  the live one, and the collector is stopped during the refresh, so the
  earlier lock-cooperation concern no longer applies.)
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

## Revision — 2026-05-31, v2.12.2 (timer-path orchestration)

The SQLite 3.45.x revision (above) noted that holding the write lock for
the full ~620 k-row reload is acceptable because
`scripts/update.sh` stops the collector before running the updater. That
was true for **deploy-time** invocations of `db_updater`, but the
**weekly timer** (`readsbstats-updater.timer` → `readsbstats-updater.service`)
runs the updater directly, bypassing `update.sh` and leaving the collector
running. On 2026-05-31 the weekly run on the Pi collided with the live
collector, exhausted the collector's `busy_timeout=30000`, produced cascading
`database is locked` errors, and was eventually killed by systemd's
`TimeoutStartSec=300` mid-`BEGIN IMMEDIATE` — the swap rolled back and the
weekly refresh did not happen.

**v2.12.2 closes the orchestration gap at the systemd unit level:**

- `readsbstats-updater.service` gains
  `ExecStartPre=+/bin/systemctl stop readsbstats-collector.service`
  and `ExecStopPost=+/bin/systemctl start readsbstats-collector.service`.
  `ExecStopPost` always runs, so even a crashed or timed-out updater
  restores the collector. The `+` prefix runs both commands as root
  (the unit's `User=readsbstats` cannot manage system units without
  polkit). Hardening flags (`CapabilityBoundingSet=`, `ProtectSystem=strict`,
  etc.) are unchanged for the main `ExecStart=` body; the `+` escape applies
  only to the two systemctl calls.
- `TimeoutStartSec=300 → 900`. Empirical Pi-4 SD/USB time for the
  single-transaction swap is 3–5 min; 900 s gives ~3× headroom for DB
  growth.
- `scripts/update.sh --full` retains its own `systemctl stop` /
  `systemctl start` bracketing the direct `runuser` invocation (it
  bypasses the unit, so the unit's `ExecStartPre`/`Post` don't fire).
  Two orchestration paths exist — timer-via-unit and deploy-via-script —
  both arrive at the same invariant (no concurrent writer during the
  IMMEDIATE window).

`improvements.md` tracks **A26-FU-2** as a deferred follow-up: a
concurrent-writer regression test for `update_aircraft_db()` that would
have caught this in CI. Deferred because reproducing the timing reliably
needs Pi-class slow storage.
