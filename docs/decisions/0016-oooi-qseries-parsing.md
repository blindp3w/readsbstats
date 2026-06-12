# 0016 — OOOI from Q-series compact reports (and what we deliberately don't parse)

Date: 2026-06-12
Status: accepted

## Context

The original OOOI parser recognised only the ARINC 620 slash-TEI Standard Message
Text (`DEP / FI …/DA …/OT 0030`). Validation against a 6.4-day live vdlm2dec dump
(13.6k messages) showed that form matches **zero** real air-side downlinks — it is
the ground-side rendering. What the feed actually carries:

- **Q-series compact reports** — labels QP (OUT), QQ (OFF), QR (ON), QS (IN);
  body `<dep ICAO×4><arr ICAO×4><HHMM>[tail]`, e.g. `LIRAEPMO2106` (~122 msgs/6 d,
  Ryanair-dominated).
- **Label-49 movement reports** — airline-defined (acarsonline lists 49 as an
  Air Canada status report; our feed shows an Etihad/LOT form
  `01DCAP    ETD159/090545OMAAEPWA` = flight/DDHHMM + dep+arr pair).

## Decision

1. **Parse Q-series by label, body anchored as 8 letters + range-valid HHMM.**
   Tail must be empty or start with whitespace/digit/slash (observed tails:
   ` 192`, `/FB   71`, `/ETA 0609`, newline + position line); a letter directly
   after the time rejects the body.
2. **QQ's second HHMM group is the OUT-time echo.** Cross-referencing QP/QQ pairs
   for the same flight proves it (`QP EPMOLGTS0409` → `QQ EPMOLGTS0420 0409`,
   5/5 pairs in the dump). A lone QQ therefore fills both `t_off` and `t_out`.
   The echo mapping is QQ-only — a second time group on QR/QS has no confirmed
   meaning and is ignored.
3. **Synthesis with a dominant-city-pair guard.** Slash-TEI events take
   precedence; otherwise QP+QQ synthesize the DEP event and QR+QS the ARR event.
   Partials are grouped by city pair and only the pair with the most distinct
   phases in the window is used (ties → newest) — a quick turnaround's next-leg
   OUT report lands inside the flight window's ±1800 s slack and would otherwise
   contaminate the current leg and flip the route chip to a false mismatch.
4. **Label 49 is a route source only.** Its DDHHMM group anchors the format but
   carries unconfirmed event semantics, so no OOOI times are derived. The pair
   fills missing `dep_icao`/`dest_icao` on events and serves as the `dsta`
   fallback. The strict regex is the safety net against other carriers'
   incompatible label-49 bodies.

## Consequences

- The flight-page OOOI card actually lights up on Q-series carriers; the API
  contract (`Vdl2OooiSummary`) is unchanged.
- Misinterpretation risk of the QQ echo on unseen carriers is bounded: the value
  is range-validated HHMM and only fills `t_out` when no QP was received.
- Future (evidence pending): QS may echo the ON time symmetrically; QQ slash
  suffixes (`/FB` fuel, `/ETA`, `/FN` flight number) and multi-line position
  tails are parseable extras.
