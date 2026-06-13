# Client-side ACARS decoding + server-side #M1BPOS positions and filed routes

- Status: ACCEPTED
- Date: 2026-06-13

## Context

On a real LOT feed (~H1-dominated), roughly 9 % of messages carry bodies that a
client-side ACARS decoder can interpret — concentrated on H1 progress/position/fault
messages, OOOI confirmations, and label-49 ETD reports. The bodies as stored are
cryptic to a human reader; decoding them inline on the Messages page significantly
improves the usefulness of the log without altering the underlying data.

Two decoding paths were evaluated:

1. **Client-side general decoding** — `@airframes/acars-decoder` (MIT, npm) covers
   the broadest set of ACARS subtypes and is actively maintained (coverage grows with
   upstream releases). It has no Pi CPU cost (runs in the browser) and fits neatly as
   a lazy chunk. On the LOT feed the measured decode rate is ~9 %; the remaining 91 %
   (full-route `#M1BPOS` bodies, `#CFBMM`/`#CFBFDE` maintenance faults, raw label-49
   ETDs) the decoder returns `decoded: false` and the raw body is shown unchanged.

2. **Server-side `#M1BPOS` parsing** — the client decoder cannot feed the server-side
   map overlay (`/api/vdl2/positions`), and it returns `decoded: false` on full-route
   `#M1BPOS` bodies (those carry a richer structured payload than the decoder expects).
   Positions and filed-route fields must therefore be extracted in Python, at query
   time, and joined into the API response. `#M1BPOS` bodies carry ddmmm-encoded lat/lon
   (comparable precision to Label-16 AUTPOS) and an `/RP:` route field
   (dep/arr/company-route/SID/STAR/approach).

## Decision

Decode ACARS bodies on **two levels**:

- **Client-side general decoding** via `@airframes/acars-decoder` — lazy-loaded as a
  separate Vite chunk on VDL2 surfaces only (`hooks/useAcarsDecoder`). `MessageList`
  renders the human-readable decoded fields above the raw body; falls back to raw when
  `decoded: false`. The raw body is always retained.
- **Server-side `#M1BPOS` parsing** in `src/readsbstats/vdl2/m1bpos.py` — applied at
  query time in the read-only VDL2 handlers. Precise positions are merged into
  `/api/vdl2/positions` (alongside Label-16 AUTPOS), and the parsed `filed_route`
  object (`dep`, `arr`, optional `company_route`/`sid`/`star`/`approach`) is attached
  to matching rows in `/api/vdl2/messages`.

VDL2 remains fully read-only over the separate `vdl2.db`; no writes to `history.db`.

## Consequences

- **Bundle:** ~39 KB gz lazy chunk (`vdl2-decoder`). Loaded only when a user opens a
  VDL2 surface; zero overhead for installations where `RSBS_VDL2_ENABLED` is unset.
- **Pi CPU:** zero added server-side cost for decoding — everything runs in the
  browser. `#M1BPOS` parsing is a lightweight regex + string slice at query time.
- **Coverage:** the decoder's label coverage grows automatically with upstream
  `@airframes/acars-decoder` releases (dependabot). Server-side `m1bpos.py` is
  maintained in-repo.
- **Remaining raw labels:** `#CFBMM`/`#CFBFDE` maintenance faults and label-49 ETDs
  are not decoded by the client library and have no server-side parser; they remain
  raw. Tracked as future work.
- **Contract coupling:** the `filed_route` object is an optional field on existing
  message rows — no breaking change to the API. The positions endpoint's `precise`
  flag already distinguished coarse/precise fixes; `#M1BPOS` points are a new source
  of `precise: true` points, transparent to callers.
