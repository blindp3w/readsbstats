# Hide superseded multi-block fragments; decode more ACARS labels locally; AF and KLM route sources

- Status: ACCEPTED
- Date: 2026-10-24

## Context

dumpvdl2 stores every block of a multi-block ACARS message (`acars.more = true`)
as its own row. When libacars reassembles the message, the final block carries
the full text, so the earlier blocks are duplicates. On 90 days of data there
were 61,741 intermediate blocks. Of the rows that carry text, 29,548 (21.7%, and
28.5% of H1 rows) are fully contained in a later final block with the same
airframe, label and message number. The final block follows its fragments within
3 s (median), 23 s (p99) and 121 s (max). None of these fragments is shorter than
20 characters. About 32k intermediate blocks have no complete copy, because
reassembly failed or the final block was lost. Those still carry content and stay
visible.

The duplicates were most visible on the map. Each `#M1BPOS` duplicate became a
separate point, so the same 604 fixes appeared as 690 points.

The feed also had many decodable message types that were shown only as raw
text: SA media advisories, B9 D-ATIS requests, 5U weather requests, Ryanair
8A/8C/8F/8S door events, Air France label-26 ETA reports and KLM label-20 `RST`
route reports.

## Decision

1. **Mark and hide duplicate fragments; never delete them.**
   - New nullable `vdl2_messages` columns: `more` (1/0 from `acars.more`; `NULL`
     off ACARS and under vdlm2dec) and `superseded` (1 when the row is hidden).
   - At ingest, in the same transaction as the insert,
     `vdl2_db.mark_superseded()` runs over the batch. It flags the `more = 1` rows
     that share `(icao_hex, label, msgno)` with a final block and fall within the
     previous 300 s, when the fragment body (at least 20 characters) is contained
     in the final body. The rule is idempotent and FTS is untouched.
   - The Messages feed and the flight ACARS panel hide superseded rows by default.
     `fragments=true` shows them (UI: "Show partial blocks"). Intermediate blocks
     carry a `partial` badge.
   - `/api/vdl2/positions` and `/api/vdl2/oooi` ignore superseded rows.
2. **Counters count received frames.** `/api/vdl2/stats`, `/timeseries`,
   `/signal`, `/active`, the health check and `has_acars` still include partial
   blocks and superseded rows. They measure the feed, not the distinct messages.
3. **Local decoders in the client, after airframes.** SA, B9, 5U, Ryanair doors,
   AF label 26 and KLM label 20 `RST` are decoded in `frontend/src/lib/acarsLocal.ts`.
   They run after the bundled `@airframes/acars-decoder` returns nothing, and they
   also run before its lazy chunk loads, because they need no import. Each was
   validated against the 90-day feed (for example SA 1,176/1,176, Ryanair doors
   1,231/1,231, label 26 142/142, KLM RST 41/41).
   - Rule: a meaning that is not published and was inferred from ADS-B is labelled
     "probable" in the UI. The Ryanair door meaning (C = closed, O = opened) is
     inferred this way.
4. **Air France and KLM routes as OOOI route sources.** `/api/vdl2/oooi` gains
   `source` values `"label26"` and `"label20"`. Precedence is
   `cm > label26 > label20 > label49`. Only the four contract keys are copied.

## Consequences

- **Un-backfilled rows still show duplicates.** The backfill
  (`scripts/backfill_vdl2_dumpvdl2.py`) fills `more` and runs the supersede pass
  on rows stored before 2.28.0. Until it runs, those rows are visible. The rest
  age out with retention.
- **Counters and messages differ on purpose.** A chart can show more messages than
  the feed lists. This is intended.
- **Ryanair door meanings are unconfirmed.** The inferred timing is strong (C comes
  17 to 34 min before the first ADS-B fix after takeoff, median; O about 10 min
  after the last fix on landing), but there is no public spec. The UI says
  "probable".
- **KLM RST routes can disagree with the route enrichment.** The two disagree in
  28 of 36 cases. ADS-B shows those aircraft descending into and landing at the
  RST destination, so the enrichment is stale: KLM reuses alphanumeric callsigns.
- **Label 26 matches the enrichment for 131 of 141 flights.** The other 10 have no
  enriched route.
- **Next-leg caveat.** The flight window used by the SPA is first seen to last seen
  plus or minus 30 min. None of these AF or KLM flights has a CM logon in that
  window. A much wider window can pick up the next leg's logon, which would win
  over the label-26 or RST route.
- **No schema change in `history.db`.** Two nullable `vdl2_messages` columns are
  added idempotently on open.

## Alternatives considered

- **Delete the duplicate rows at ingest.** Rejected. It is irreversible, and it
  loses the per-block frequency, signal and raw JSON.
- **Detect duplicates at query time.** Rejected. It would need the raw JSON of
  every candidate row on every request.
- **Decode the new labels only on the server.** Rejected for the display
  decoders. They are small regex parsers over text the client already has, and
  keeping them client-side matches ADR-0017 and needs no API change. The server
  does parse labels 26 and 20, but only to feed the OOOI `route`. Those two
  regexes therefore exist on both sides, which is the accepted cost of keeping
  display decoding in the client.
- **Trust the route enrichment over RST.** Rejected. ADS-B shows the RST route is
  correct where the two disagree.
