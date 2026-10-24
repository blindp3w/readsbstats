# Precise positions and reported routes from more ACARS sources

- Status: ACCEPTED
- Date: 2026-10-24

## Context

Precise VDL2 map positions came from two bodies: Label-16 AUTPOS and `#M1BPOS`.
Many airframes send their position under other labels. Wizz Air uses label 17,
Finnair uses label 38, and FANS AFN logons (label B0) and ADS-C reports (label B6)
carry positions too. Those fixes were either dropped or not shown at all.

The ATN Context-Management (CM) logon is a different case. dumpvdl2 decodes it
as the aircraft's own FMS flight id, departure and destination. Before 2.27.0
the normalizer stored these as content-free rows, so the aircraft's reported
route was not available to the flight page. The route in the flight's scheduled
data comes from an enrichment service, which is sometimes wrong.

A new format is easy to get wrong. Some label-17 rows carry the departure
airport's coordinates, and label 2F is in hundredths of a decimal degree, not
degrees and minutes. A generic coordinate regex over all labels would accept
both.

## Decision

1. **Precise positions only from an explicit label-to-parser whitelist.**
   `vdl2/positions.py` maps each label to its own parser
   (`PRECISE_LABEL_PARSERS`). A coordinate-looking body under any other label is
   never trusted.
2. **A new format is admitted only after validation against ADS-B.** Each
   candidate was compared with ADS-B tracks of the same airframe, using the
   nearest ADS-B fix within ±60 s of the receive time, over 90 days of data.
   The table below is the result.

   | Source | rows | median | p90 |
   |---|---|---|---|
   | label 16 AUTPOS (existing) | 2,501 | 0.35 nm | 1.7 nm |
   | label 17 (Wizz Air) | 1,603 | 0.86 nm | 2.1 nm |
   | labels 10/13/18/27/33/12/43/41/2P (label-16 style) | 106 | 0.69 nm | 2.2 nm |
   | label 38 (Finnair, longitude first) | 205 | 1.00 nm | 2.3 nm |
   | label 2F (hundredths of a decimal degree) | 73 | 0.50 nm | 0.9 nm |
   | label 14 (`LAT N … LON E …`) | 61 | 0.74 nm | 1.0 nm |
   | label B0 FANS AFN logon (minutes×10) | 28 | 0.09 nm | 0.3 nm |
   | ADS-C basic report (label B6, from `raw`) | 114 | 0.11 nm | 0.6 nm |

   Two rules came out of that validation:
   - Label-17 rows whose altitude field is `------` carry the departure
     airport's coordinates (7 rows, up to about 1,200 nm off). Such rows are
     rejected, so a label-17 fix is accepted only when the altitude is an integer.
   - Label 2F read as degrees and minutes was about 19.5 nm off. It is decoded
     as decimal.
3. **ADS-C is parsed from `raw` at query time.** The basic report is not in
   the body. It is decoded by dumpvdl2 into the `raw` JSON, so the position
   read reads that JSON. No new column is needed.
4. **ATN CM logon is lifted at ingest.** The normalizer stores
   `label='CM'`, `body='LOGON <callsign> <dep>-<dest>'` and `dsta` = destination.
   - `flight` stays NULL. The logon carries an ICAO callsign, while the stats
     code's top-airlines count assumes IATA ACARS flight ids. Storing the
     callsign there would mix the two.
   - The CM route wins over an airline label-49 movement report for the same
     flight, because the logon comes from the aircraft's FMS.
   - When a flight has several logons, the newest one in the flight window is
     used.

## Consequences

- **Residual noise in label-16 positions.** About 0.4% of label-16 fixes (9 of
  about 2,355 matched) are more than 20 nm off. No format signature separates
  them, so they stay. The map shows them as precise points.
- **Coverage is per label.** A new airline format needs its own validation
  before it is added to the whitelist.
- **CM rows are visible.** They appear in the message feed and in the
  top-labels stats, at roughly 60 a day on a busy receiver.
- **Route agreement is a useful signal.** Over 90 days, 5,276 ADS-B flights had
  a logon, and 567 of them had no route from the route enrichment. Where both
  sources exist they agree 77% of the time. The matches / differs chip on the
  card is meaningful. The newest logon in the window matched the flight's
  callsign 99.8% of the time.
- **Backfill needed for old rows.** Rows stored before 2.27.0 keep NULL
  `label`, `body` and `dsta` until the backfill runs
  (`scripts/backfill_vdl2_dumpvdl2.py`). It is idempotent and safe while the
  ingest service runs. The rest age out with retention.
- **No schema change.** Positions read from existing columns and `raw`. CM rows
  reuse `label`, `body` and `dsta`.

## Alternatives considered

- **A generic coordinate regex on all labels.** Rejected. It would accept the
  label-17 rows that carry departure-airport coordinates, and it would mistake
  label 2F for degrees and minutes. Each new label would be trusted without
  checking.
- **A new `dep` column for the logon.** Rejected. The departure fits in the
  body, and a strict body parser (`parse_cm_logon`) is enough. A new column would
  need a schema change for no added accuracy.
