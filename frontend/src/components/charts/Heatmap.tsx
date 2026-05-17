// Day-of-week × Hour activity heatmap. CSS-grid based; same approach as
// v1's stats.js. Each cell's opacity is normalised to the max value in the
// dataset, with a 0.18 floor so low-but-nonzero cells remain visible (v1
// improvements.md #11 — addressed the "invisible 1-2 flight cells" gap).

import { CHART_COLORS } from './theme';
import { SimpleTooltip } from '@/components/ui/Tooltip';

interface HeatmapRow {
  dow: number; // 0=Sun .. 6=Sat
  hour: number; // 0..23
  count: number;
}

const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const HOURS = Array.from({ length: 24 }, (_, i) => i);

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
    <div className="overflow-x-auto" data-testid="activity-heatmap">
      <div
        className="grid gap-px rounded border border-[var(--color-border-default)] p-1 text-xs"
        style={{
          gridTemplateColumns: 'auto repeat(24, minmax(18px, 1fr))',
        }}
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
          <div
            key={label}
            className="contents"
            // eslint-disable-next-line react/no-unknown-property
            data-row={d}
          >
            <div className="pr-1 text-right text-[var(--color-text-dim)]">{label}</div>
            {HOURS.map((h) => {
              const count = grid[d][h];
              const opacity = max === 0 ? 0 : count === 0 ? 0 : Math.max(0.18, count / max);
              return (
                <SimpleTooltip key={h} content={`${label} ${h}:00 — ${count} flights`}>
                  <div
                    tabIndex={0}
                    className="h-5 rounded-sm outline outline-1 outline-[var(--color-border-default)]/40 focus:outline-2 focus:outline-[var(--color-accent)]"
                    style={{
                      background:
                        count === 0
                          ? 'transparent'
                          : `${CHART_COLORS.accent}${alphaHex(opacity)}`,
                    }}
                    aria-label={`${label} ${h}:00 ${count} flights`}
                  />
                </SimpleTooltip>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function alphaHex(a: number): string {
  return Math.round(Math.min(1, Math.max(0, a)) * 255)
    .toString(16)
    .padStart(2, '0');
}
