# Single long-lived Telegram dispatch consumer thread

- Status: ACCEPTED
- Date: 2026-05-11

## Context

The collector polls aircraft every 5 seconds. The previous design spawned a new daemon thread per poll to deliver Telegram notifications. A photo download + Telegram upload takes up to ~23 seconds per alert. Under bursty alert conditions (e.g., 10 military aircraft spotted in one poll), this spawned 10 concurrent threads all doing HTTP work, contending on the database, and running past the next poll cycle.

## Decision

Replace per-poll thread spawning with a single long-lived `tg-dispatch` consumer thread started once in `collector.main()` after `READY=1`. Alerts are enqueued onto `collector._notification_queue`. The consumer processes them serially, opening one sqlite connection at thread start (`notifier._thread_local.conn`) and reusing it for every alert.

## Consequences

- Decouples photo download + upload latency from the 5-second poll loop.
- Eliminates daemon-thread pileup under bursty conditions.
- Serial processing means alerts arrive in order, never concurrently.
- Tests use `collector._drain_notifications(timeout=...)` to wait for the queue to empty — never join the thread.
- The consumer's sqlite connection is separate from the main collector connection; no locking issues.
