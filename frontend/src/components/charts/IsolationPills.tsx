// Pill row above a multi-series time-series chart that lets the user
// click to ISOLATE one series (others fade) — extracted from the v2.8.0
// Metrics aircraft-count panel so the Flight detail page (M3.2) can
// reuse the exact same pattern + styling.
//
// The chart itself owns the visual fade (via the option builder's
// `isolated?: string | null` arg → mutates per-series opacity). This
// component is purely the HTML pill row + selection state plumbing.

import { CHART_COLORS } from '@/components/charts/theme';
import { cn } from '@/lib/cn';

interface Props {
  // Stable string keys per series (e.g. 'ac_adsb', 'alt'). NOT
  // unit-dependent labels — those go in `labels` so toggling units
  // doesn't break the isolation lookup.
  keys: string[];
  // Display labels per pill. May change when the user toggles units.
  labels: string[];
  // Color dot per pill. Should match the chart's series color.
  colors: string[];
  // Which key (if any) is currently isolated. null = all series at
  // full opacity.
  isolated: string | null;
  onChange: (next: string | null) => void;
  // Prefix for data-testid attrs:
  //   wrapper      → `{testIdPrefix}-pills`
  //   per-pill     → `{testIdPrefix}-pill-{k}`
  // Preserves v2.8.0 Metrics testids when prefix='metrics-aircraft'.
  testIdPrefix: string;
}

export function IsolationPills({ keys, labels, colors, isolated, onChange, testIdPrefix }: Props) {
  return (
    <div
      role="group"
      aria-label="Series isolation"
      className="flex flex-wrap items-center gap-2"
      data-testid={`${testIdPrefix}-pills`}
    >
      {keys.map((k, i) => {
        const active = isolated === k;
        const color = colors[i] ?? CHART_COLORS.accent;
        return (
          <button
            key={k}
            type="button"
            onClick={() => onChange(active ? null : k)}
            aria-pressed={active}
            aria-label={`Isolate ${labels[i] ?? k}`}
            data-testid={`${testIdPrefix}-pill-${k}`}
            className={cn(
              // 44 px tap target on mobile (Apple HIG), 36 px on
              // desktop where pointer precision makes the smaller
              // pill less awkward.
              'inline-flex min-h-[44px] items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors md:min-h-9',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
              active
                ? 'border-[var(--color-accent)] bg-[var(--color-surface-2)] text-[var(--color-text)]'
                : 'border-[var(--color-border-default)] text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)]/60 hover:text-[var(--color-text)]',
            )}
          >
            <span
              aria-hidden="true"
              className="inline-block h-2.5 w-2.5 rounded-full"
              style={{ background: color }}
            />
            {labels[i] ?? k}
          </button>
        );
      })}
    </div>
  );
}
