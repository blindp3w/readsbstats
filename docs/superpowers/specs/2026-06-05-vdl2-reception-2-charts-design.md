# VDL2 / ACARS reception → two range-driven charts — design

**Date:** 2026-06-05
**Branch:** `feat/vdl2-integrations` (PR #11)
**Status:** Approved design, ready for implementation plan.

## Context & goal

The Metrics page currently shows VDL2/ACARS reception as a single `Vdl2ReceptionCard`
with KPI tiles + a per-frequency mini-table + one tiny 60-minute sparkline. The user
wants it redesigned as **two real charts**, styled to match the rest of the Metrics page
(ECharts), and driven by the page's existing time-range picker.

Confirmed decisions (from brainstorming):

- **Two charts:** (1) total **message rate** as an area line; (2) **per-frequency
  activity** as **small multiples** — one stacked sub-panel per frequency, exactly the
  style of the existing "Signal quality (dBFS)" panel (left label, right current value,
  shared time axis, x-labels only on the bottom row).
- **Time window:** wired to the Metrics page's **existing `RangePicker`** (24h / 7d / 30d /
  all) — changing the range moves the readsb panels and the VDL2 charts together.
- **Frequencies:** **dynamic top-6 by volume** in the window (ordered desc), not the
  hardcoded LOT channels — handles any band plan / receiver tuning.

## Architecture

### Backend — `GET /api/vdl2/timeseries?from=&to=`

New read-only endpoint in `src/readsbstats/api/vdl2.py` (plain `def`, wrapped in
`_vdl2_guard()`, cached ~30 s). Returns the **same columnar shape as `/api/metrics`** so
the frontend reuses the existing chart builders unchanged:

```json
{
  "bucket_seconds": 60,
  "metrics": ["rate", "136.725", "136.875", "136.975", "136.775"],
  "freqs":   [136.725, 136.875, 136.975, 136.775],
  "total":   556,
  "data": [ [t0, t1, …], [rate0, rate1, …], [f1_0, f1_1, …], … ]
}
```

`total` is the raw message **count** in the window (for the header "N msgs"); the series
values are normalized msgs/min and must NOT be summed to get a count.

Behaviour:

- **Params:** `from`/`to` epoch seconds (aliases, matching `/api/metrics`); default `to=now`,
  `from=now-86400`. Validate `to > from` (else 400, like the message endpoints).
- **Bucket selection** by span — mirrors `/api/metrics` thresholds but with a **60 s
  minimum** (rows are individual messages, so there is no "raw" mode):
  | span | bucket_seconds |
  |---|---|
  | ≤ 24 h | 60 |
  | ≤ 7 d | 300 |
  | ≤ 30 d | 900 |
  | ≤ 90 d | 3600 |
  | > 90 d | 14400 |
- **Top frequencies:** one query for the top-6 `ROUND(freq,3)` by `COUNT(*)` in the window
  (`freq IS NOT NULL`), ordered desc → defines the per-freq series + their order.
- **Bucketed counts:** `GROUP BY (ts/B)*B` for the total (`rate`) and per-frequency counts
  (`GROUP BY bucket, ROUND(freq,3)` restricted to the top freqs), pivoted in Python.
- **Normalize to msgs/min:** every series value = `count * 60 / bucket_seconds`, so the
  y-axis stays comparable as buckets coarsen. (Rounded to 1–2 dp.)
- **Zero-fill** every bucket in `[from, to)` so quiet bins read `0` (not interpolated gaps).
- **Index:** the window scan is served by `idx_vdl2_ts (ts DESC)`. vdl2_messages is small
  (hundreds–few-thousand rows/day), so per-minute bucketing over 24h is cheap; wider ranges
  use coarser buckets. No new index needed.

### Frontend — `Vdl2ReceptionCard` rebuilt as two charts

`frontend/src/components/metrics/Vdl2ReceptionCard.tsx`:

- New props `from: number; to: number` (passed from `Metrics.tsx`'s existing range state,
  the same `from`/`to` it already gives `/api/metrics`). Keeps `enabled` self-gating
  (returns `null` when not available).
- Fetch `/api/vdl2/timeseries?from&to`, matching the page's `/api/metrics` query behaviour:
  query key includes `from`/`to`, `staleTime` ~30 s, `placeholderData: prev`, refetch on
  range change. No separate auto-poll (consistent with the readsb panels).
- **Chart 1 (rate):** `buildPanelOption(resp, ['rate'], [amber], …)` → `<EChart height=…>`.
- **Chart 2 (per-freq small multiples):** `buildSignalSmallMultiplesOption(resp, resp.metrics.slice(1), colors, labels, fmtAxisTime, fmtAxisDate, fmtTs)` where labels are the freq strings ("136.725") → `<EChart height = SMALL_MULT_HEIGHT * nFreqs …>`. Colors from `CHART_COLORS`/the channel palette.
- **Slim header:** "last message X ago" freshness from the already-seeded `['health']` query
  (`vdl2.newest_age_sec`, with the same `STALE_SEC` ⚠ treatment) + "N msgs" in the window
  (the response's `total`). The current KPI tiles, per-freq table, and embedded sparkline are
  removed.
- `Metrics.tsx`: pass `from`/`to` into `<Vdl2ReceptionCard>` (still behind `vdl2Available`).

### Cleanup / removals (all added in this same unmerged branch — safe to replace)

- Remove `GET /api/vdl2/reception`, `_compute_reception`, `_rate_buckets` (if unused
  elsewhere — it is only used by reception), and the reception slow-query log.
- Remove `Vdl2ReceptionResponse` + `Vdl2FreqStat` schemas and the matching TS types; add
  `Vdl2TimeseriesResponse` (or reuse the generic `MetricsResp` TS type the panels use).
- Remove the reception backend tests (`TestReception`) and `vdl2-reception.test.tsx`
  assertions that no longer apply; replace with timeseries + new-card tests.
- Add `.superpowers/` to `.gitignore` (visual-companion artifacts).

## Testing

**Backend (`tests/test_api_vdl2.py`, test-first):**
- bucket selection per span (60/300/900/3600/14400) at the threshold boundaries;
- msgs/min normalization (e.g. 5 msgs in a 300 s bucket → 1.0/min);
- zero-fill: a quiet bucket in the middle of the window is `0`, grid length matches the
  span/bucket;
- dynamic top-6: with >6 distinct freqs, only the top 6 by volume appear, ordered desc;
  the `rate` series totals ALL messages (not just the top 6);
- `to <= from` → 400; DB-unavailable → 503 (via `_vdl2_guard`).

**Frontend (`frontend/test/`):**
- both `<EChart>` charts render from a mocked columnar response (EChart is mockable like the
  existing chart tests; assert the option/series shape or chart testids);
- header freshness shows "X ago" / ⚠ stale from mocked `/api/health`;
- changing `from`/`to` issues a new fetch;
- card renders nothing when `enabled` is false.

**End-to-end:** local uvicorn with a seeded `vdl2.db` → `curl /api/vdl2/timeseries?from&to`
asserts shape; on the Pi, the Metrics page shows the two charts and they move with the range
picker.

## Out of scope

- Per-message-type (label) breakdown charts — only rate + per-frequency for now.
- Signal-level series — vdlm2dec emits none (dumpvdl2-only; tracked separately).
- A standalone VDL2 range picker — the charts share the page's existing one.

## Reuse summary (why this is low-risk)

The columnar `{bucket_seconds, metrics, data}` contract is the linchpin: it lets
`buildPanelOption` and `buildSignalSmallMultiplesOption` (already shipped, already tested)
drive both charts with **no new charting code** — the work is one backend aggregation
endpoint plus rewiring the card to the page's range state.
