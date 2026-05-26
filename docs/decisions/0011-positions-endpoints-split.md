# Split /api/flights/{id} positions into three endpoints

- Status: ACCEPTED
- Date: 2026-05-26

## Context

`GET /api/flights/{id}` returned the entire ordered `positions` list
inline in its JSON payload, with no LIMIT or downsampling. The
audit on 2026-05-26 flagged this as a performance problem: long
flights or high-frequency feeds produced multi-MB responses, slow
chart rendering, and visible jank on the Pi when ECharts had to
render every sample.

`FlightProfileChart` and `RouteMap` only need a visually faithful
silhouette of the flight — they do not need every sample. The
inspection `PositionTable` displays raw rows but already
client-side-samples to 500 entries to keep its DOM small.

## Decision

Three endpoints, all read-only `def` handlers (per `src/CLAUDE.md`):

1. **`GET /api/flights/{id}`** — unchanged shape. Still embeds the
   full `positions` list. Kept as-is so any external consumer
   (mobile app, scraper) keeps working.
2. **`GET /api/flights/{id}/positions?limit=&offset=`** — paginated
   raw positions ordered by `ts`. Default 1000, max 2000. Backed by
   the new `idx_positions_flight_ts` composite (ADR-0010 sibling /
   audit C3a).
3. **`GET /api/flights/{id}/positions/chart?target=`** —
   LTTB-downsampled positions for chart/map rendering. Default
   target 500, max 2000. Implemented in
   `src/readsbstats/downsample.py`.

The frontend (`Flight.tsx`) issues a `target=500` query for the
altitude/speed chart and a separate `target=2000` query for the route
map. `PositionTable` continues to read from the embedded `positions`
field of `/api/flights/{id}` — its existing client-side sampler is
sufficient and migrating it would only increase request fan-out.

## Why LTTB

The "Largest-Triangle-Three-Buckets" algorithm preserves visible
peaks, troughs, and slope changes by maximising the triangle area
each picked point forms with its neighbours. It is the de-facto
standard for visual downsampling in charting libraries (Highcharts,
ECharts upstream, etc.). The 50-LOC pure-Python implementation here
runs in well under 50 ms on the Pi for 10 k-row flights.

LTTB chooses indices, not values, so the same selection drives the
chart, map polyline, and any future overlay — all parallel series
stay row-aligned without extra logic.

## Alternatives considered

- **Hard cap at the existing endpoint.** Breaks any external
  consumer that depends on receiving every position. The
  audit explicitly flagged this risk.
- **Server-side decimation (every Nth row).** Simpler but visually
  worse: a flat cruise segment swallows peaks at the bucket
  boundary. LTTB pays a small CPU cost (~10 ms per 10 k samples) to
  fix this.
- **Pre-computed downsamples in a sidecar column or table.** Avoids
  per-request work but adds storage and a migration path. The
  request rate doesn't justify it.

## Consequences

- The embedded `positions` field on `/api/flights/{id}` stays full
  size. A follow-up release may deprecate / cap it once we are
  confident no external consumer relies on the legacy shape.
- LTTB's bucketing assumes input is sorted by `x` (ts). The SQL
  `ORDER BY ts` is the source of truth; if the schema ever loses
  the ordering guarantee, the chart shape will silently degrade.
