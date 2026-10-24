# dumpvdl2 H1 sublabel/MFI as columns; body kept verbatim

- Status: ACCEPTED
- Date: 2026-10-24

## Context

Since the VDL2 ingest moved to the dumpvdl2 decoder, libacars strips the H1 text
prefix from ACARS bodies and reports it as separate JSON fields: `acars.sublabel`
(the `M1` of a downlink `#M1B`) and `acars.mfi` (the `B0` of an optional `/B0 `).
The normalizer dropped both. It also ignored the XID `vdl_params` `ac_location`
(aircraft position, 0.1° lat/lon, altitude in feet) and `dst_airport`.

Several features key on that prefix or on those fields, so they went empty for
dumpvdl2 data: `#M1BPOS` precise positions and `/RP:` filed routes, the H1 body
kind chips, the coarse XID map points, and the `dsta` destination fallback in
the OOOI card. vdlm2dec feeds were not affected, because their body still
carries the prefix.

## Decision

1. **Store the prefix parts in columns.** `vdl2_messages` gets nullable
   `sublabel` and `mfi` columns, added idempotently on open like `sig_level` and
   `noise_level`. The normalizer fills them from `acars.sublabel` / `acars.mfi`.
2. **Keep `body` exactly as the decoder emitted it.** The prefix is not written
   back into the text.
3. **Rebuild the prefix at read time.** `normalize.acars_text(body, sublabel,
   mfi)` restores `#<sublabel>B[/<mfi> ]` in front of the body. The prefix-keyed
   parsers (`m1bpos`, route extraction) and the UI kind chips use it. Responses
   return the body as stored.
4. **Map XID fields.** `ac_location` feeds `lat`, `lon` and `alt`. `dst_airport`
   feeds `dsta`. `gs_location` (the ground station) is never used.
5. **Body-only FTS update trigger.** `vdl2_au` fires only on `UPDATE OF body`, so
   the non-body backfill updates skip FTS reindexing. The writer swaps legacy
   triggers on open.
6. **Backfill old rows.** `scripts/backfill_vdl2_dumpvdl2.py` re-runs the
   normalizer over stored `raw` JSON and fills only NULL columns. It is idempotent
   and safe while the ingest service runs.

## Consequences

- **Full-text search drifts from the decoder text.** Rows from dumpvdl2 do not
  contain the `#M1B` prefix in `body`, so an FTS query for the literal `#M1BPOS`
  does not match them. Searches on the message text still work, and the sublabel
  is available as a field.
- **Uplink prefix is not rebuilt.** The uplink form (`- #SL…`) is not reproduced.
  The receiver only hears aircraft, so no uplink rows are expected to need it.
- **Multi-block M1 messages may yield two positions.** A `#M1BPOS` message that
  spans blocks can produce two precise points. Deduplicating them is out of scope
  for this change.
- **Schema change is additive.** Two nullable columns, no table rebuild. Rows
  stored before the change have NULLs until the backfill runs; the rest age out
  with retention.
- **Fidelity is preserved.** `body` and `raw` stay as the decoder produced them,
  so the stored data can be re-parsed later without guessing what was stripped.

## Alternatives considered

- **Rewrite `body` with the prefix restored.** Rejected. The client-side
  `@airframes/acars-decoder` stops recognising OHMA messages once the prefix is
  back in the text (all of them carry sublabel `T1`), and this gains no decode
  coverage. It would also change the FTS index contents and the stored body away
  from the decoder output.
- **Backfill only, no normalizer change.** Rejected. The one-shot script repairs
  existing rows, but new ingest would keep dropping the fields, so the problem
  returns on every new message.
