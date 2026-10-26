# VDL2 frozen at 2.29.0

- Status: ACCEPTED
- Date: 2026-10-26

## Context

The SDR that feeds the VDL2 ingest is needed for something else, so the maintainer
turns VDL2 off on their receiver. The feature is complete for its purpose: ingest
and storage ([ADR-0013](0013-vdl2-timeseries-reuses-metrics-columnar-contract.md)),
OOOI times and client-side decoding
([ADR-0016](0016-oooi-qseries-parsing.md), [ADR-0017](0017-client-side-acars-decoding.md)),
dumpvdl2 sublabel columns ([ADR-0018](0018-dumpvdl2-sublabel-columns.md)), precise
positions and reported routes ([ADR-0019](0019-vdl2-position-and-route-sources.md)),
and multi-block fragments plus local decoders
([ADR-0020](0020-vdl2-multiblock-fragments-and-local-decoders.md)). Further VDL2
work cannot be validated without a live feed, and 2.29.0 fixes the remaining
robustness and performance bugs found before the freeze.

## Decision

1. **Freeze development at 2.29.0.** No new decoders, label parsers or message
   formats.
2. **Keep the code, opt-in and maintained for bugs and security only.** Nothing is
   removed. The feature stays behind `RSBS_VDL2_ENABLED` (default off) and keeps
   its separate `vdl2.db`.
3. **Turning it off must be safe and documented.** With the flag off the web server
   never opens or ATTACHes `vdl2.db`, the `/api/vdl2/*` router is not registered,
   every VDL2 surface in the SPA hides itself, and `readsbstats-vdl2` exits cleanly
   after sending `READY`. `history.db` is never touched by VDL2 and no alert depends
   on it. The step-by-step procedure lives in
   [Operations → Turning VDL2 off](../operations.md#turning-vdl2-off).
4. **The decoder default is now `dumpvdl2`** (it was `vdlm2dec`). dumpvdl2 is the
   recommended decoder and the only one that supports signal level, CPDLC, CM
   logons and ADS-C. Operators running vdlm2dec set `RSBS_VDL2_DECODER=vdlm2dec`.

## Consequences

- **Index rule.** Cheap `vdl2.db` indexes stay in `_DDL_INDEXES`, which `migrate()`
  creates on open (web startup, each new web thread, and the ingest before it reports
  ready). Heavy indexes that need a full-table pass go in `_DDL_HEAVY_INDEXES` and
  are built by the ingest service in a background thread after `READY=1`
  (`idx_vdl2_label_icao`, `idx_vdl2_flight`). Building one reads the whole table
  (about 0.7 GB at 90-day retention) from the Pi's USB disk, which must not stall a
  web request or the ingest start (systemd `TimeoutStartSec=120`).
- **Stats are slow until the heavy indexes exist.** The top-labels and top-airlines
  aggregates took 2–4 minutes on a cold cache before the indexes; they are cached and
  concurrent misses share one computation. The Messages tab uses
  `/api/vdl2/stats?summary=true`, which reads only the headline counts.
- **Upgrade note.** Anyone running vdlm2dec must now set `RSBS_VDL2_DECODER=vdlm2dec`.
  If datagrams arrive but nothing normalizes, the ingest logs a one-time warning.
- **Turning it off leaves data behind.** `vdl2.db` stays on disk and retention pruning
  stops, because pruning runs in the ingest service. Deleting `vdl2.db*` reclaims the
  space.

### Known limitations at the freeze

- The summary log line and the idle flush run only when the socket has been idle for
  1 s, so under continuous traffic they are delayed.
- The UDP listener is IPv4-only.
- ADS-C positions need the stored raw JSON to fit `RSBS_VDL2_RAW_MAX` (8192).
- The Aircraft page ACARS panel shows the latest 100 messages, with no "load older".
- Nothing runs `VACUUM` on `vdl2.db`; retention frees pages but the file does not shrink.
- The web process still runs the idempotent schema `migrate()` on `vdl2.db` at startup
  and per thread (cheap indexes only) while the flag is on.
- With the flag on and `vdl2.db` deleted, the web server recreates an empty database.

## Resuming

Set `RSBS_VDL2_ENABLED=true` in `/etc/readsbstats/vdl2.env`, restart
`readsbstats-web`, run `systemctl enable --now readsbstats-vdl2` and start the
decoder. Nothing else needs migrating: the schema is upgraded idempotently on open,
and the ingest builds the heavy indexes on its first start. New work would start by
re-checking the known limitations above and adding any new format only after
validating it against ADS-B tracks, as in ADR-0019.

## Alternatives considered

- **Remove VDL2 from the code base.** Rejected. The feature works, is opt-in and
  isolated in its own database, and removal would discard tested code for no gain
  while the flag already turns every surface off.
- **Keep developing without a live feed.** Rejected. New parsers cannot be validated
  against real traffic and ADS-B tracks, which ADR-0019 requires.
