// Formatting helpers. Ports of static/js/units.js + base.html inline globals
// (escHtml/fmtTs/fmtDur/fmtBytes).
//
// All unit-dependent helpers take `units` as an explicit parameter so React
// (and the React Compiler) sees the dependency. Components should call them
// via the `useFormat()` hook (hooks/useFormat.ts), which subscribes to the
// units store so re-renders trigger automatically when the user toggles
// units in the nav.
//
// Module-level helpers that call `getUnits()` (the non-reactive snapshot
// accessor) live in `store/units.ts` for use OUTSIDE React (e.g. CSV download
// URLs); do NOT call those from render code.

import type { UnitSystem } from '@/store/units';
import { getClockFormat, type ClockFormat } from '@/store/clockFormat';

const KTS_TO_KMH = 1.852;
const KTS_TO_MPH = 1.15078;
const NM_TO_KM = 1.852;
const NM_TO_MI = 1.15078;
const FT_TO_M = 0.3048;

export function fmtAlt(ft: number | null | undefined, units: UnitSystem, showUnit = true): string {
  if (ft == null) return '—';
  if (units === 'metric') {
    return Math.round(ft * FT_TO_M).toLocaleString() + (showUnit ? ' m' : '');
  }
  return Math.round(ft).toLocaleString() + (showUnit ? ' ft' : '');
}

export function fmtSpd(kts: number | null | undefined, units: UnitSystem, showUnit = true): string {
  if (kts == null) return '—';
  if (units === 'metric')
    return Math.round(kts * KTS_TO_KMH).toLocaleString() + (showUnit ? ' km/h' : '');
  if (units === 'imperial')
    return Math.round(kts * KTS_TO_MPH).toLocaleString() + (showUnit ? ' mph' : '');
  return Math.round(kts).toString() + (showUnit ? ' kts' : '');
}

export function fmtDist(nm: number | null | undefined, units: UnitSystem, showUnit = true): string {
  if (nm == null) return '—';
  if (units === 'metric') return (nm * NM_TO_KM).toFixed(1) + (showUnit ? ' km' : '');
  if (units === 'imperial') return (nm * NM_TO_MI).toFixed(1) + (showUnit ? ' mi' : '');
  return nm.toFixed(1) + (showUnit ? ' nm' : '');
}

export function altLabel(units: UnitSystem): string {
  return units === 'metric' ? 'Alt (m)' : 'Alt (ft)';
}
export function spdLabel(units: UnitSystem): string {
  return units === 'metric' ? 'Speed (km/h)' : units === 'imperial' ? 'Speed (mph)' : 'Speed (kts)';
}
export function distLabel(units: UnitSystem): string {
  return units === 'metric' ? 'Dist (km)' : units === 'imperial' ? 'Dist (mi)' : 'Dist (nm)';
}

// Time / duration / bytes — port of base.html inline globals. Unit-independent.

// Default clockFormat = getClockFormat() so non-render callers (CSV export,
// notifier proxies) automatically pick up the user's setting. The reactive
// path is via useFormat().fmtTs which subscribes to the store.
export function fmtTs(
  epoch: number | null | undefined,
  clockFormat: ClockFormat = getClockFormat(),
): string {
  if (!epoch) return '—';
  return new Date(epoch * 1000).toLocaleString(undefined, {
    hour12: clockFormat === '12h',
  });
}

// Short HH:MM form for chart axis ticks. Full timestamps from `fmtTs` are too
// long for time-axis ticks at hourly resolution — ECharts hides overlapping
// labels and the axis ends up blank. Use this for axes; keep `fmtTs` for
// tooltips, where the full datetime is welcome.
export function fmtAxisTime(
  epoch: number | null | undefined,
  clockFormat: ClockFormat = getClockFormat(),
): string {
  if (epoch == null) return '';
  return new Date(epoch * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    hour12: clockFormat === '12h',
  });
}

// Locale-aware short date (day + month) for multi-day axis ranges, where
// every tick at midnight would otherwise read "00:00" via `fmtAxisTime`.
// Locale picks the order: DD/MM in en-GB / pl-PL / most of EU; MM/DD in
// en-US. Skip clockFormat — it doesn't apply to date-only ticks.
export function fmtAxisDate(epoch: number | null | undefined): string {
  if (epoch == null) return '';
  return new Date(epoch * 1000).toLocaleDateString([], {
    day: '2-digit',
    month: '2-digit',
  });
}

export function fmtDur(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${h}h ${mm}m` : `${h}h`;
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function fmtAgo(epoch: number | null | undefined, now: number = Date.now() / 1000): string {
  if (!epoch) return '—';
  const dt = now - epoch;
  if (dt < 60) return `${Math.round(dt)}s ago`;
  if (dt < 3600) return `${Math.round(dt / 60)}m ago`;
  if (dt < 86400) return `${Math.round(dt / 3600)}h ago`;
  return `${Math.round(dt / 86400)}d ago`;
}
