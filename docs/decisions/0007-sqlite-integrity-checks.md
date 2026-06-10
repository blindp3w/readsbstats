# SQLite crash-safety hardening: synchronous=FULL + integrity checks

- Status: ACCEPTED (decision 1, `synchronous = FULL`, superseded 2026-06-11 — default is `NORMAL` again; decisions 2–3 stand)
- Date: 2026-05-19 (revised 2026-06-11: `synchronous = FULL` superseded by DB performance overhaul Phase 1)

## Context

A power outage hit the Pi. The DB survived intact (WAL mode protected it), but
investigation surfaced three gaps:

1. `synchronous=NORMAL` in WAL mode skips the per-commit WAL fsync — committed
   transactions can be silently lost on power cut. USB HDDs commonly lie about
   `SYNCHRONIZE CACHE` (the SCSI equivalent of fsync), so this matters here.
2. No startup check detects corruption that WAL auto-recovery cannot fix
   (malformed main-DB pages written before the crash).
3. No periodic check catches slow-developing corruption between outages.

The XFS mount on `/mnt/ext` has write barriers enabled by default; no
mitigation needed at the filesystem layer.

## Decision

**1. `synchronous = FULL`** in both `database.DDL` and `database.connect()`.
This adds one fsync per commit (a few extra ms on USB HDD; negligible for the
5-second poll loop) and guarantees that committed transactions survive power
loss when the underlying disk respects fsync.

**Revision (2026-06-11, DB performance overhaul Phase 1): superseded.** The
default is `synchronous = NORMAL` again, with a new `RSBS_DB_SYNCHRONOUS`
env var (`FULL` | `NORMAL`) to restore the FULL behavior. Two things changed
since this ADR. First, the original cost estimate ("a few extra ms") was
wrong: the production USB HDD was measured at ~67 ms per flush, paid on
every commit. Second, the durability/cost framing was re-examined: WAL +
NORMAL is already corruption-safe — a power cut can lose at most the last
few committed transactions, never the database itself — so FULL only bought
durability for the final few seconds of position data, which for a tracker
re-polling live aircraft every 5 s is not worth a 67 ms fsync per write
(user-approved tradeoff). Operators who prefer per-commit durability set
`RSBS_DB_SYNCHRONOUS=FULL`. Decisions 2 and 3 below (dirty-shutdown
sentinel, integrity-check timers) are unaffected and remain in force.

**2. Dirty-shutdown sentinel** at `<DB_PATH>.parent/.dirty_shutdown`. The
collector writes it on startup and deletes it on graceful shutdown. On the
next startup, if the sentinel is still present, the collector runs
`PRAGMA quick_check(10)` and, on success, `PRAGMA wal_checkpoint(TRUNCATE)`
to clean up. On corruption, it logs CRITICAL but continues (degraded) rather
than refuse to start — observability over availability is the wrong trade-off
for a 24/7 unattended Pi.

**3. Two systemd timers** for proactive checks:

- `readsbstats-dbcheck.timer` — `Sun 03:30` local, runs `PRAGMA quick_check`.
- `readsbstats-dbcheck-full.timer` — `1st Sun 04:00`, runs
  `PRAGMA integrity_check` (catches index/table divergence quick_check misses).

03:00–04:00 local was chosen from 90 days of `receiver_stats`: average 0.3–0.6
aircraft visible (the absolute trough). Blocking the writer briefly during a
check is harmless at that hour.

Both timer services use `OnFailure=notify-telegram@%n.service` so corruption
detected at 03:30 on a Sunday surfaces immediately, not buried in journalctl.

`scripts/check_db.py` is the shared entrypoint: opens read-only via
`?mode=ro` URI (safe against the live writer per WAL semantics), exits 0 on
pass, 1 on corruption, 2 on open/query error.

## Consequences

- One extra fsync per write commit. Throughput impact negligible on a 5-second
  poll cadence; measurably worse on a USB HDD only if the disk respects fsync,
  which is the whole point.
- Dirty-shutdown sentinel costs zero on normal restarts. On an unclean restart
  it adds the quick_check time (~6 s on 700 MB DB) plus up to `busy_timeout`
  (30 s) for `wal_checkpoint(TRUNCATE)` to clear active readers.
- Periodic checks scale with DB size. At current growth, quick_check stays
  under 1 min for ~5 GB; integrity_check stays under 5 min for ~5 GB. Beyond
  that, switch the monthly check to `quick_check` only or sample-based.
- Known limitation: if SQLite cannot open the DB at all (rare, header damaged),
  `database.init_db()` raises before the integrity check helper can run.
  Systemd retries, eventually OnFailure fires the Telegram alert — generic
  "service failed" rather than "DB corruption detected". Acceptable.
