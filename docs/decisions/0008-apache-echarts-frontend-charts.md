# Apache ECharts as the SPA chart library

- Status: ACCEPTED
- Date: 2026-05-19

## Context

The v2.0.0 SPA shipped with Recharts as the chart library across four pages:
`/metrics` (11 stacked panels), `/stats` (hourly + daily bars + the top-N
TopChart), `/flight/:id` (altitude + speed dual-axis profile). Recharts is
SVG-based and the `/metrics` 11-panel grid at the 7d / 30d / 90d ranges was
emitting tens of thousands of DOM nodes, slowing the first paint and the
scroll behaviour.

A v2.1 backlog item ("ECharts canvas + LTTB for `/metrics` time-series")
was queued for this swap from the original UI redesign plan. We widened
scope in the same release to cover all four chart surfaces so the
`recharts` dependency and chunk could go away cleanly.

## Decision

Migrate to **Apache ECharts 6** on a **canvas renderer**, tree-shaken via
`echarts/core` + explicit component imports.

- No third-party React wrapper. `echarts-for-react@3` was the natural pick
  but pulls `size-sensor` as a transitive dep, which is currently flagged
  as malware ([GHSA-gx6x-v325-85g4](https://github.com/advisories/GHSA-gx6x-v325-85g4)).
  We ship our own ~60-line wrapper at `frontend/src/components/charts/EChart.tsx`
  hand-rolled on `echarts/core` (`echarts.init` + `ResizeObserver` + dispose).
- Components imported: `LineChart`, `BarChart`, `GridComponent`,
  `TooltipComponent`, `DataZoomComponent`, `LegendComponent`,
  `CanvasRenderer`. No `MarkLineComponent` or `LabelLayout` — neither is
  used.
- Cross-panel sync on `/metrics` via `echarts.connect('metrics')` called
  from each chart's `onChartReady` (idempotent; new instances join the
  group as they mount).
- Per-panel `dataZoom: 'inside'` for wheel/pinch zoom on time-series.
- `series.sampling: 'lttb'` on every line series — kicks in when the
  point count exceeds the rendered pixel width.
- Axis time formatter is span-aware: < 36 h → `HH:MM`; ≥ 36 h →
  locale-aware `DD/MM`. The on-hover axis-pointer label keeps the full
  timestamp via `useFormat().fmtTs` so the 12h / 24h `RSBS_TIME_FORMAT`
  preference is preserved.

`Heatmap.tsx` (DOW × hour) and `PolarRange.tsx` were **kept as custom
SVG/CSS**. Heatmap relies on per-cell DOM affordances (Radix tooltip,
keyboard focus, per-cell `aria-label`) that ECharts canvas cannot
preserve. Polar is 127 lines of clean working SVG with no readability
issue worth the +8 KB gz that ECharts' polar coordinate system would
cost.

## Consequences

What becomes easier:
- High-density time series on `/metrics` render on canvas — no more
  DOM-node blow-up at the 30d / 90d ranges.
- Cross-panel hover sync surfaces correlated spikes (CPU vs message rate
  vs aircraft drop) at a glance.
- Per-panel zoom without an API round-trip.
- LTTB downsampling is a single config flag, not a separate library.
- Future chart needs (heatmaps, sunbursts, polar variants) are already
  in the same library.

What becomes harder / costs:
- Bundle: `charts-*.js` grew from **112 KB gz → 188 KB gz** (~+76 KB gz)
  — ECharts core + zrender + 2 chart types + 4 components + canvas
  renderer. The chunk is lazy-loaded by `/stats`, `/metrics`, `/flight`
  only; shell and other pages are unaffected. Net page-load cost on
  affected pages is the trade for canvas perf + new affordances.
- Custom wrapper means we own the lifecycle code (init / setOption /
  dispose / resize / event bind) rather than delegating to a maintained
  package. The wrapper is ~60 lines and unit-tested.
- Recharts' declarative `<Bar>` / `<Line>` JSX is gone — option objects
  are imperative configs. Each chart now has a `build*Option` exported
  helper for unit testing.
