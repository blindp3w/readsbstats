// Day-of-week × Hour activity heatmap. CSS-grid based; each cell is bucketed
// into one of 5 stops from HEATMAP_RAMP (see ./theme) based on its share of
// the dataset's max. Discrete buckets, not a continuous gradient, so
// neighbouring cells with similar values still read as distinct. Empty
// (count=0) cells render transparent.
//
// Two responsive layouts, gated purely by Tailwind:
//   < sm  (≤ 639 px): hours as ROWS (24), days as COLS (7).
//                    Fits iPhone 15 portrait (393 px) without horizontal
//                    scroll. 7 × ~50 px ≈ 350 px.
//   ≥ sm  (≥ 640 px): hours as COLS (24), days as ROWS (7).
//                    Original layout — the wider chart reads better at
//                    laptop / tablet widths.
//
// DOM per cell is preserved across both layouts so Radix tooltips,
// keyboard focus, and per-cell aria-labels keep working (this is the
// a11y win that kept us off ECharts canvas in ADR-0008).

import { HEATMAP_RAMP } from './theme';
import { SimpleTooltip } from '@/components/ui/Tooltip';
import { rampColor } from './chartMath';

interface HeatmapRow {
  dow: number; // 0=Sun .. 6=Sat
  hour: number; // 0..23
  count: number;
}

const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const HOURS = Array.from({ length: 24 }, (_, i) => i);

function Cell({
  count,
  max,
  day,
  hour,
}: {
  count: number;
  max: number;
  day: number;
  hour: number;
}) {
  const label = `${DOW[day]} ${hour}:00`;
  return (
    <SimpleTooltip content={`${label} — ${count} flights`}>
      <div
        tabIndex={0}
        className="h-5 rounded-sm outline outline-1 outline-[var(--color-border-default)]/40 focus:outline-2 focus:outline-[var(--color-accent)]"
        style={{ background: rampColor(count, max) }}
        aria-label={`${label} ${count} flights`}
      />
    </SimpleTooltip>
  );
}

export function ActivityHeatmap({ rows }: { rows: HeatmapRow[] }) {
  // Index by [dow][hour] for O(1) lookup; safer than Map for a small grid.
  const grid: number[][] = Array.from({ length: 7 }, () => Array(24).fill(0));
  let max = 0;
  for (const r of rows) {
    if (r.dow >= 0 && r.dow < 7 && r.hour >= 0 && r.hour < 24) {
      grid[r.dow][r.hour] = r.count;
      if (r.count > max) max = r.count;
    }
  }

  return (
    <div data-testid="activity-heatmap">
      {/* < sm: hours as rows, days as 7 columns. */}
      <div
        className="grid gap-px rounded border border-[var(--color-border-default)] p-1 text-xs sm:hidden"
        style={{ gridTemplateColumns: 'auto repeat(7, minmax(0, 1fr))' }}
        data-layout="mobile"
      >
        <div />
        {DOW.map((label) => (
          <div key={label} className="text-center text-[10px] text-[var(--color-text-dim)]">
            {label}
          </div>
        ))}
        {HOURS.map((h) => (
          <div key={`row-${h}`} className="contents" data-row-hour={h}>
            <div className="pr-1 text-right text-[10px] text-[var(--color-text-dim)] tabnum">
              {h % 3 === 0 ? h : ''}
            </div>
            {DOW.map((_, d) => (
              <Cell key={`m-${d}-${h}`} count={grid[d][h]} max={max} day={d} hour={h} />
            ))}
          </div>
        ))}
      </div>

      {/* ≥ sm: hours as 24 columns, days as 7 rows. Wrap in overflow-x-auto
          as a defensive fallback if the parent narrows below the
          minmax(18px) floor on a desktop browser resize. */}
      <div className="hidden overflow-x-auto sm:block" data-layout="desktop">
        <div
          className="grid gap-px rounded border border-[var(--color-border-default)] p-1 text-xs"
          style={{ gridTemplateColumns: 'auto repeat(24, minmax(18px, 1fr))' }}
        >
          <div />
          {HOURS.map((h) => (
            <div
              key={h}
              className="text-center text-[10px] text-[var(--color-text-dim)] tabnum"
              style={{ minWidth: 18 }}
            >
              {h % 3 === 0 ? h : ''}
            </div>
          ))}
          {DOW.map((label, d) => (
            <div key={label} className="contents" data-row={d}>
              <div className="pr-1 text-right text-[var(--color-text-dim)]">{label}</div>
              {HOURS.map((h) => (
                <Cell key={`d-${d}-${h}`} count={grid[d][h]} max={max} day={d} hour={h} />
              ))}
            </div>
          ))}
        </div>
      </div>

      {max > 0 && (
        <div
          className="mt-2 flex items-center gap-1.5 text-[10px] text-[var(--color-text-dim)] tabnum"
          aria-hidden="true"
          data-testid="activity-heatmap-legend"
        >
          <span className="mr-1">1</span>
          {HEATMAP_RAMP.map((c) => (
            <span key={c} className="inline-block h-2.5 w-5 rounded-sm" style={{ background: c }} />
          ))}
          <span className="ml-1">{max.toLocaleString()}</span>
          <span className="ml-1">flights/hr</span>
        </div>
      )}
    </div>
  );
}
