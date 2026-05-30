// URL-state hook for the time-range picker. Extracted from RangePicker.tsx
// so that file only exports the component itself — react-refresh hygiene.
//
// Two storage shapes:
//   ?range=24h|7d|30d|90d|all   — relative-to-now window
//   ?from=<epoch>&to=<epoch>     — explicit absolute window (Custom)
//
// Custom takes precedence over `range`. When the user picks a preset, the
// from/to params are cleared from the URL so the page stays bookmarkable.

import { useSearchParams } from 'react-router-dom';

export type RangeValue = 'all' | '24h' | '7d' | '30d' | '90d' | 'custom';

const PRESET_VALUES = new Set<RangeValue>(['24h', '7d', '30d', '90d', 'all']);

export interface RangeState {
  // Active preset (used to color the toggle). 'custom' is implied when from/to
  // are present without an explicit `range=custom`.
  value: RangeValue;
  // Resolved absolute window (always present for non-'all').
  from?: number;
  to?: number;
}

export function useRange(defaultValue: RangeValue = '24h'): {
  state: RangeState;
  setPreset: (v: RangeValue) => void;
  setCustom: (from: number, to: number) => void;
  clearCustom: () => void;
} {
  const [params, setParams] = useSearchParams();
  const fromRaw = params.get('from');
  const toRaw = params.get('to');
  const rangeRaw = params.get('range') as RangeValue | null;

  const hasCustom = fromRaw != null && toRaw != null;
  let value: RangeValue;
  if (hasCustom) value = 'custom';
  else if (rangeRaw && PRESET_VALUES.has(rangeRaw)) value = rangeRaw;
  else value = defaultValue;

  let from: number | undefined;
  let to: number | undefined;
  if (hasCustom) {
    from = Number(fromRaw);
    to = Number(toRaw);
    if (!Number.isFinite(from) || !Number.isFinite(to)) {
      from = undefined;
      to = undefined;
    }
  } else {
    const w = presetWindow(value);
    from = w.from;
    to = w.to;
  }

  const setPreset = (v: RangeValue) => {
    setParams((prev) => {
      const out = new URLSearchParams(prev);
      out.delete('from');
      out.delete('to');
      if (v === defaultValue) out.delete('range');
      else out.set('range', v);
      return out;
    });
  };

  const setCustom = (from_: number, to_: number) => {
    setParams((prev) => {
      const out = new URLSearchParams(prev);
      out.set('from', String(Math.floor(from_)));
      out.set('to', String(Math.floor(to_)));
      out.delete('range');
      return out;
    });
  };

  const clearCustom = () => setPreset(defaultValue);

  return { state: { value, from, to }, setPreset, setCustom, clearCustom };
}

function presetWindow(range: RangeValue): { from?: number; to?: number } {
  if (range === 'all' || range === 'custom') return {};
  const now = Math.floor(Date.now() / 1000);
  const sec =
    range === '24h'
      ? 86400
      : range === '7d'
        ? 7 * 86400
        : range === '30d'
          ? 30 * 86400
          : 90 * 86400;
  return { from: now - sec, to: now };
}
