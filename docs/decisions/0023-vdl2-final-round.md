# VDL2 final round, re-frozen at 2.33.0

- Status: ACCEPTED
- Date: 2026-11-23

## Context

[ADR-0021](0021-vdl2-frozen.md) froze VDL2 at 2.29.0 and named the precondition for
any further format work: a new format is admitted only after it is validated against
ADS-B tracks on real traffic ([ADR-0019](0019-vdl2-position-and-route-sources.md)).
A 90-day snapshot of `vdl2.db` now makes that check possible. The client decoder
`@airframes/acars-decoder` was also bumped from 1.9.3 to 1.9.7.

Before wrapping VDL2 up for good, three reviews ran: a backend audit, a frontend audit,
and mining of real traffic. They found bugs and several position and route formats
that could be checked against ADS-B.

Validation used a 90-day `vdl2.db` snapshot (468,556 messages, 2026-08-25 to
2026-11-23) against ADS-B history (positions up to 2026-11-09). The method is the one
from ADR-0019: for each fix, the nearest ADS-B position of the same airframe within
±60 s of the fix time. The ship gate is median ≤ 3 nm and p90 ≤ 6 nm.

## Decision

This is the last VDL2 round. It supersedes [ADR-0021](0021-vdl2-frozen.md) for this
round only. The freeze rules of ADR-0021 apply again from 2.33.0.

1. **Bugs fixed.**
   - One out-of-range number in a datagram (for example an altitude of 1e300 or a
     hugely negative timestamp) overflowed the SQLite integer bind. That dropped the
     whole ingest batch, and the backfill crashed on every re-run. Integers outside
     int64 now become NULL. Negative timestamps use the receive time.
   - `/api/vdl2/*` parameters ≥ 2^63 returned 500. They now return 422 through upper
     bounds.
   - The web server recreated an empty `vdl2.db` after the file was deleted. It now
     opens the file with `mode=rw` and reports VDL2 unavailable until the ingest
     creates the file. This resolves the ADR-0021 limitation on recreation.
   - CSV export ignored `has_acars`.
   - Frontend: the Messages page polled while VDL2 was unavailable. Every visible row
     was re-decoded on each scroll render, and each component built its own decoder.
     There is now one shared decoder, with results cached per message id and one retry
     on chunk load failure, followed by a visible hint. Decodes that only echoed the
     body (label B6 shown as "CPDLC Message") count as undecoded. Long decoded values
     no longer overflow rows. Times are labelled UTC.

2. **New position sources.** They are query-time parsers, not stored columns. Each
   was checked against ADS-B as described above.

   | Source | Format | Time base | n | median nm | p90 nm |
   |---|---|---|---|---|---|
   | H1/DF ABS/EB0/SB0 | CSV dialect (N/S ddmm.m, E/W dddmm.m, DDHHMM, alt, temp, wind) | embedded DDHHMM | 19,332 | 1.26 | 3.61 |
   | H1/DF ABS/EB0 | fixed width (lat/lon ×1000 columns, HHMM, alt) | embedded HHMM | 3,122 | 2.14 | 4.46 |
   | H1/CF FDE/MM | decimal | embedded HHMMSS | 914 | 0.20 | 0.40 |
   | H1 POS- | ddmm, HHMMSS, FL | embedded | 91 | 0.95 | 2.00 |
   | label 83 | PR form (ddmm.m) | embedded | 68 | 0.12 | 0.24 |
   | label 83 | ET form (decimal) | receive | 114 | 0.20 | 0.42 |
   | label 15 | FST01 | receive | 88 | 1.23 | 2.39 |
   | label 80 | POSRPT | receive | 11 | 0.09 | 0.16 |

   - **Time base matters for ABS.** One message carries many minutes of fixes. Using
     the receive time gives 1.61 / 9.22 nm (CSV dialect) and 4.55 / 17.16 nm (fixed
     width), so every ABS fix uses its own embedded time. One ABS message can therefore
     yield several map points.
   - **ADS-C points** also carry track, ground speed and vertical speed from
     `earth_ref_data`.
   - **Per-flight endpoint.** `GET /api/vdl2/positions/{icao_hex}?since&until` (window
     at most 48 h) feeds the Flight page overlay and a winds / temperatures-aloft
     table. The winds table covers the CSV dialect only.

3. **New route sources** for the Flight-page route. They sit below the existing
   precedence, in this order: cm > label26 > label20 > label49 > r64 > abs > b44 > fst01.

   | Source | Agreement with callsign-based route enrichment |
   |---|---|
   | R64 | 100% (n=200) |
   | ABS CSV dialect | 87.2% (2,765) |
   | ABS fixed width | 86.4% (1,196) |
   | B44 | 84.8% (682) |
   | FST01 | 61.2% (67) |

   The mismatches were inspected. Most were enrichment errors: charter callsigns mapped
   to the wrong leg, a stale airport code where ACARS reports the current one, and a
   scheduled callsign enriched with the wrong destination for an aircraft seen locally.
   The ACARS-reported route is therefore treated as authoritative, but it keeps the
   lowest precedence.

4. **Visualisation.** Map popups and a legend for VDL2 points. ACARS fixes on the
   Flight page route map. An OOOI block-time timeline. Structured rendering of decoded
   items (positions, routes, landing data, fuel). A 24 h ground-station table (per
   ground-station address and frequency: frames, aircraft, mean SNR; dumpvdl2
   downlinks only). A decoded-only filter.

5. **Not done, with reasons.**
   - CPDLC placeholder values: the CPDLC summary is built at ingest, so a fix would
     apply to new rows only.
   - XID handoff map: the data has no ground-station coordinates.
   - ADS-C predicted route: 135 messages, low value.
   - DF TKO, `/AB`, A32 REP and historic APM/CRZ snapshots: low volume, or positions
     not tied to a time that can be validated.
   - M1 PRG winds: there is no server parser.
   - Map deep links from decoded chips.

6. **Decoder pin.** The `vdl2-decoder` chunk is about 53 KB gzipped against a 60 KB
   budget. `acars-decoder` 1.10 adds about 30% (roughly 68 KB gzipped, over budget) and
   ships OOOI and QE plugins that overlap the server's own OOOI parsing. Dependabot
   ignores minor and major updates of this package. Any update requires re-running the
   guard tests (local formats must stay undecoded by the library) and raising the budget
   deliberately.

7. **MCP: no change.** The MCP server has no VDL2 tools. `flights.has_acars` already
   passes through.

## Consequences

- **Re-frozen at 2.33.0** under the ADR-0021 rules. Further formats still need
  validation against ADS-B tracks, as in ADR-0019.
- **Findings that are not bugs.**
  - About 25k intermediate DF blocks stay unmarked. A block was never received, and
    libacars could not reassemble the message, which points to lost RF frames.
  - 63 H1 rows keep an unstripped `sequence+#DFB` prefix. The impact is negligible.

### Known limitations at the freeze

- The summary log line and the idle flush run only when the socket has been idle for
  1 s, so under continuous traffic they are delayed.
- The UDP listener is IPv4-only.
- ADS-C positions need the stored raw JSON to fit `RSBS_VDL2_RAW_MAX` (8192).
- The Aircraft page ACARS panel shows the latest 100 messages, with no "load older".
- Nothing runs `VACUUM` on `vdl2.db`; retention frees pages but the file does not shrink.
- The web process still runs the idempotent schema `migrate()` on `vdl2.db` at startup
  and per thread (cheap indexes only) while the flag is on.

## Alternatives considered

- **Keep the freeze and fix bugs only.** Rejected. The formats could be validated now
  against a real snapshot, which ADR-0021 named as the precondition.
- **Gate routes at ≥ 90% agreement with enrichment.** Rejected. Enrichment is the less
  reliable side, as the mismatch review showed.
- **Adopt `acars-decoder` 1.10.** Rejected for now. It exceeds the chunk budget, and its
  OOOI and QE plugins overlap the server's OOOI parsing.
