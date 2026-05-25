// One-line context sentence under the range picker: "Showing **last 24
// hours** · 2026-05-19 11:00 → 2026-05-20 11:00 · vs previous 24h".
// Includes an inline refreshing spinner aligned right.

import type { RangeState, RangeValue } from '@/components/RangePicker';
import { useFormat } from '@/hooks/useFormat';

const PRESET_TITLE: Record<RangeValue, string> = {
  '24h': 'last 24 hours',
  '7d': 'last 7 days',
  '30d': 'last 30 days',
  '90d': 'last 90 days',
  all: 'all time',
  custom: 'custom range',
};

const PREV_LABEL: Partial<Record<RangeValue, string>> = {
  '24h': 'previous 24h',
  '7d': 'previous 7d',
};

interface Props {
  state: RangeState;
  isFetching?: boolean;
}

export function RangeContextLine({ state, isFetching }: Props) {
  const { fmtTs, fmtDate } = useFormat();
  const title = PRESET_TITLE[state.value];
  const prev = PREV_LABEL[state.value];

  // Day-granular windows don't need time in the range tail — operator
  // doesn't care that the 30d window started at 14:50 on April 25th.
  // Sub-day ranges (24h, sub-day custom) keep HH:MM precision.
  const span = state.from != null && state.to != null ? state.to - state.from : 0;
  const useDateOnly =
    state.value === '7d' ||
    state.value === '30d' ||
    state.value === '90d' ||
    state.value === 'all' ||
    (state.value === 'custom' && span >= 86400);
  const fmt = useDateOnly ? fmtDate : fmtTs;

  return (
    <div
      className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-[var(--color-text-dim)]"
      data-testid="stats-range-context"
    >
      <span>
        Showing <strong className="text-[var(--color-text)]">{title}</strong>
      </span>
      {state.from && state.to ? (
        <span className="tabnum">
          · {fmt(state.from)} → {fmt(state.to)}
        </span>
      ) : null}
      {prev ? (
        <span>
          · compared with <strong className="text-[var(--color-text)]">{prev}</strong>
        </span>
      ) : null}
      {isFetching ? (
        <span className="ml-auto inline-flex items-center gap-1" aria-live="polite">
          <svg
            aria-hidden="true"
            className="h-3 w-3 animate-spin text-[var(--color-text-dim)]"
            viewBox="0 0 16 16"
            fill="none"
          >
            <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5" opacity="0.25" />
            <path d="M14 8a6 6 0 0 0-6-6" stroke="currentColor" strokeWidth="1.5" />
          </svg>
          <span>refreshing</span>
        </span>
      ) : null}
    </div>
  );
}
