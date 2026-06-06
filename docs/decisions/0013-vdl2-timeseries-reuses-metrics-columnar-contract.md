# VDL2 reception charts reuse the /api/metrics columnar contract

- Status: ACCEPTED
- Date: 2026-06-05

## Context

The Metrics page originally showed VDL2/ACARS reception as a single
`Vdl2ReceptionCard` with KPI tiles, a per-frequency mini-table, and one
small 60-minute sparkline (the dedicated `GET /api/vdl2/reception`
endpoint). The goal for 2.15.0 was to replace that with two real charts —
a total **message-rate** line and a **per-frequency** activity panel —
styled to match the rest of the Metrics page (Apache ECharts, per
ADR-0008) and driven by the page's existing time-range picker.

The readsb metrics on the same page already render through two shared
ECharts builders, `buildPanelOption` and `buildSignalSmallMultiplesOption`
(the latter being the "Signal quality (dBFS)" small-multiples style). Both
consume a columnar response shape from `/api/metrics`:

```json
{ "bucket_seconds": 60, "metrics": ["rate", "136.725", …], "data": [[t0,t1,…],[v0,v1,…],…] }
```

## Decision

Add a new read-only `GET /api/vdl2/timeseries?from=&to=` (plain `def`,
`_vdl2_guard()`-wrapped, like the other VDL2 read endpoints) that aggregates
`vdl2_messages` into time buckets and returns the **same columnar shape as
`/api/metrics`**. The frontend then drives both VDL2 charts with the
existing `buildPanelOption` / `buildSignalSmallMultiplesOption` builders —
**no new charting code**. `Vdl2ReceptionCard` shares `Metrics.tsx`'s
`from`/`to` range state, so the range picker moves the readsb panels and
the VDL2 charts together.

Aggregation specifics (see `docs/api.md` for the contract):

- **Bucket by span**, mirroring `/api/metrics` thresholds but with a 60 s
  floor (rows are individual messages — there is no "raw" mode):
  60 / 300 / 900 / 3600 / 14400 s for ≤24 h / ≤7 d / ≤30 d / ≤90 d / more.
- **msgs/min normalization** — every series value is `count * 60 /
  bucket_seconds`, so the y-axis stays comparable as buckets coarsen.
  Series therefore must **not** be summed to recover a raw count; the
  header count comes from a separate `total` field.
- **Dynamic top-6 frequencies** by volume in the window (ordered desc),
  not hardcoded channels — adapts to any band plan / receiver tuning.
- **Zero-filled** buckets so quiet bins read `0`, not interpolated gaps.
- **Window capped** at ~366 days (400 otherwise) to bound bucket-grid
  memory.

The old `GET /api/vdl2/reception` endpoint, its `_compute_reception`
helper, and the `Vdl2ReceptionResponse`/`Vdl2FreqStat` schemas were
removed in the same (unreleased) feature branch.

## Why reuse the contract

The columnar `{bucket_seconds, metrics, data}` shape is the linchpin: it
makes the two reception charts a pure backend-aggregation problem. The
already-shipped, already-tested chart builders are reused verbatim, so the
charts inherit the page's exact look (shared time axis, x-labels only on
the bottom small-multiples row, current-value labels) for free, and there
is no second charting code path to keep in sync with the readsb panels.

## Alternatives considered

- **Keep the KPI/table/sparkline card.** Rejected — it did not match the
  rest of the Metrics page and could not honor the shared range picker.
- **A bespoke VDL2 chart component + response shape.** Rejected — it would
  duplicate the small-multiples rendering and drift from the readsb panels
  over time. Conforming the endpoint to the existing contract is strictly
  less code.
- **A standalone VDL2 range picker.** Rejected — two pickers on one page
  is confusing; sharing the page's `from`/`to` is simpler and expected.

## Consequences

- The endpoint is coupled to the `/api/metrics` columnar contract: a
  breaking change there (e.g. a different `data` layout) would need to be
  mirrored here or the shared builders forked. This is an accepted, low
  cost given both live in this repo.
- **vdlm2dec-only — no signal level.** Unlike the readsb "Signal quality
  (dBFS)" panel, the per-frequency panel plots msgs/min, because vdlm2dec
  emits no per-message signal level (only dumpvdl2 does; tracked
  separately). The shared small-multiples builder is signal-agnostic, so
  this required no builder change.
